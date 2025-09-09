# app.py
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel, HttpUrl, Field
from typing import Optional, Any, Dict, List, Literal,NamedTuple
import os,requests, urllib.parse, json, time, uuid, threading
from audit_client import (
    get_site,
    get_audit_for_id,  
    get_status_for_id, load_auditoria, filter_audit_since
)
import ssl
from requests.adapters import HTTPAdapter
from urllib3 import PoolManager
import certifi

AIRE_CA_BUNDLE = os.getenv("AIRE_CA_BUNDLE", "/app/aire_ca_bundle.pem")

Operator = Literal["afinia","aire"]


class UnsafeAdapter(HTTPAdapter):
    """Adapter that also supports legacy renegotiation; can be made fully insecure if requested."""
    def __init__(self, *, insecure: bool = False, **kwargs):
        self.insecure = insecure
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        if self.insecure:
            # Fully insecure context (hostname off + no cert verification)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        else:
            # Normal verification context
            ctx = ssl.create_default_context()
            ctx.check_hostname = True
            ctx.verify_mode = ssl.CERT_REQUIRED
        # Allow legacy renegotiation (needed by servicios.air-e.com)
        ctx.options |= 0x4  # SSL_OP_LEGACY_SERVER_CONNECT
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

def make_session_for_operator(operator: str, insecure_verify: bool = False) -> requests.Session:
    s = requests.Session()
    if operator == "aire":
        # Mount adapter for ALL https calls; choose secure (default) or fully insecure (last resort)
        s.mount("https://", UnsafeAdapter(insecure=insecure_verify))
        if not insecure_verify:
            # keep verification ON with a reliable CA bundle
            s.verify = AIRE_CA_BUNDLE
    else:
        # Afinia: standard TLS verification
        s.verify = certifi.where()
    return s

class Site(NamedTuple):
    base: str            # scheme+host
    root_path: str       # "/Autogeneracion" (Afinia) or "/CREG174" (Air-e)
    home_referer: str    # base + root_path + "/"
    form_page: str       # root_path + "/form/WFSolicitud.aspx"
    consulta_page: str   # root_path + "/WFConsulta.aspx"
    ua: str = "Mozilla/5.0"

def get_site(op: Operator) -> Site:
    if op == "aire":
        base = "https://servicios.air-e.com"
        root = "/CREG174"
    else:  # default "afinia"
        base = "https://servicios.energiacaribemar.co"
        root = "/Autogeneracion"
    return Site(
        base=base,
        root_path=root,
        home_referer=f"{base}{root}/",
        form_page=f"{root}/form/WFSolicitud.aspx",
        consulta_page=f"{root}/WFConsulta.aspx",
    )



app = FastAPI(title="Afinia/Aire Range Scanner")



class ScanIn(BaseModel):
    start_id: int = Field(..., description="Inclusive start ID, e.g., 40000")
    end_id: int = Field(..., description="Inclusive end ID, e.g., 41000")
    webhook_url: HttpUrl = Field(..., description="Where to POST incremental results")
    # Strategy controls how we traverse:
    # - "checkpoint": your heuristic (10/30/50/70/90) with sequential expansion on hits
    # - "linear": old linear scan (optional fallback)
    strategy: str = Field("checkpoint", pattern="^(checkpoint|linear)$")
    # Only valid IDs fetch full data; invalids only validated (fast)
    fetch_data_for_valid: bool = True
    delay_ms: int = Field(200, ge=0, description="Optional per-ID delay")
    max_retries: int = Field(2, ge=0, description="Retries per HTTP call")
    timeout_s: int = Field(30, ge=5, description="HTTP timeout seconds")
    operator: Operator = Field("afinia", description="Which operator: 'afinia' or 'aire'")

# ---------- utilities ----------
def _unwrap_d(obj: Any) -> Any:
    if isinstance(obj, dict) and "d" in obj:
        payload = obj["d"]
        if isinstance(payload, str):
            t = payload.strip()
            if (t.startswith("{") and t.endswith("}")) or (t.startswith("[") and t.endswith("]")):
                try:
                    return json.loads(t)
                except Exception:
                    return payload
        return payload
    return obj

def _boolish(x: Any) -> bool:
    if isinstance(x, bool):
        return x
    if isinstance(x, str):
        t = x.strip().lower()
        if t in ("true","1","si","sí","ok","yes","y"):
            return True
        if t in ("false","0","no","null",""):
            return False
    return bool(x)

def _notify(webhook_url: str, event: str, payload: Dict[str,Any]):
    try:
        requests.post(
            webhook_url,
            json={"event": event, **payload},
            timeout=10,
            headers={"Content-Type": "application/json"},
        )
    except Exception:
        pass

# ---------- ID navigation helpers (your heuristic) ----------
_CHECKPOINT_LAST2 = (10, 30, 50, 70, 90)

def is_checkpoint_id(i: int) -> bool:
    return (i % 100) in _CHECKPOINT_LAST2

def next_checkpoint_id(i: int) -> int:
    """Return the smallest j >= i whose last two digits are in {10,30,50,70,90}.
       If i itself is a checkpoint, returns i."""
    last2 = i % 100
    for c in _CHECKPOINT_LAST2:
        if last2 <= c:
            return i + (c - last2)
    # wrap to the next hundred
    return (i - last2) + 100 + _CHECKPOINT_LAST2[0]

# ---------- low-level HTTP calls ----------
def _post_json_with_retries(s: requests.Session, url: str, *, json_body: Any,
                            headers: Dict[str,str], timeout: int, retries: int):
    last = None
    for _ in range(retries+1):
        try:
            r = s.post(url, headers=headers, json=json_body, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(0.5)
    raise last

def _get_with_retries(s: requests.Session, url: str, *, headers: Dict[str,str],
                      timeout: int, retries: int):
    last = None
    for _ in range(retries+1):
        try:
            r = s.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(0.5)
    raise last

# ---------- fast validity check ----------
def validate_only(s: requests.Session, *, site: Site, id_solicitud: str, timeout_s: int, max_retries: int):
    headers = {
        "User-Agent": site.ua,
        "Origin": site.base,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json; charset=UTF-8",
        "Referer": site.home_referer,
    }
    r = _post_json_with_retries(
        s, f"{site.base}{site.consulta_page}/ValidaSolicitud",
        json_body={"ID_SOLICITUD": id_solicitud, "EMAIL": ""},
        headers=headers, timeout=timeout_s, retries=max_retries
    )
    raw = _unwrap_d(r.json())
    valid = (str(raw).strip().lower() in ("true", "1", "si", "sí", "ok", "yes", "y")) if isinstance(raw, str) else bool(raw)
    return {"valid": valid, "valida_raw": raw}

# ---------- full load for valid IDs ----------
def load_valid_id_full(s: requests.Session, *,site: Site, id_solicitud: str,
                       timeout_s: int, max_retries: int) -> Dict[str, Any]:
    

    headers = {
        "User-Agent": site.ua,
        "Origin": site.base,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json; charset=UTF-8",
        "Referer": site.home_referer,
    }


    # We already did ValidaSolicitud in the caller, but doing it again is cheap; skip here.

    # Encrypt
    r2 = _post_json_with_retries(
        s, f"{site.base}{site.consulta_page}/Encryptar",
        json_body={"strParameter": f"ID_SOLICITUD={id_solicitud}"},
        headers=headers,
        timeout=timeout_s,
        retries=max_retries,
    )
    enc_qs_raw = _unwrap_d(r2.json())  # "?enc=..."
    if not isinstance(enc_qs_raw, str) or not enc_qs_raw.startswith("?enc="):
        return {"warning": "Encryptar unexpected payload", "encryptar_raw": enc_qs_raw}

    # GET form to prime page-level state
    enc_qs_encoded = urllib.parse.quote(enc_qs_raw, safe="=?&")
    form_url = f"{site.base}{site.form_page}{enc_qs_encoded}"
    _get_with_retries(
        s, form_url,
        headers={"User-Agent": site.ua},
        timeout=timeout_s, retries=max_retries
    )

    # Page method: CargarDatosSolicitud (force zero-length body)
    url_cargar = f"{site.base}{site.form_page}/CargarDatosSolicitud"
    r4 = s.post(
        url_cargar,
        headers=headers,
        timeout=timeout_s,
        data=b"",  # Content-Length: 0
    )
    r4.raise_for_status()
    data_raw = _unwrap_d(r4.json())
    return {"enc_query": enc_qs_raw, "referer_used": form_url, "data": data_raw}

# ---------- background job implementing your heuristic ----------
def run_scan(job_id: str, cfg: ScanIn):
    _notify(cfg.webhook_url, "scan_started", {
        "job_id": job_id,
        "range": {"start": cfg.start_id, "end": cfg.end_id, "strategy": cfg.strategy}
    })
    site = get_site(cfg.operator)

    
    s: Optional[requests.Session] = None
    processed = found = skipped = errors = 0

    try:
        s = make_session_for_operator(cfg.operator,False)  # <-- use our factory
        if cfg.strategy == "linear":
            # Fallback: simple linear scan (optional)
            i = cfg.start_id
            step = 1 if cfg.start_id <= cfg.end_id else -1
            while (i <= cfg.end_id) if step > 0 else (i >= cfg.end_id):
                id_str = str(i)
                try:
                    vres = validate_only(s, site=site,id_solicitud=id_str,
                                         timeout_s=cfg.timeout_s, max_retries=cfg.max_retries)
                    if vres["valid"] and cfg.fetch_data_for_valid:
                        full = load_valid_id_full(s, site=site,id_solicitud=id_str,
                                                  timeout_s=cfg.timeout_s, max_retries=cfg.max_retries)
                        _notify(cfg.webhook_url, "item", {"job_id": job_id, "id": id_str,
                                                          "valid": True, "operator":cfg.operator, **full})
                        found += 1
                    else:
                        #_notify(cfg.webhook_url, "item", {"job_id": job_id, "id": id_str,
                        #                                  "valid": False, "reason": "ValidaSolicitud returned false",
                        #                                  "valida_raw": vres["valida_raw"]})
                        skipped += 1
                    processed += 1
                except Exception as e:
                    errors += 1
                    _notify(cfg.webhook_url, "item_error", {"job_id": job_id, "id": id_str, "error": str(e)})
                finally:
                    if cfg.delay_ms: time.sleep(cfg.delay_ms/1000.0)
                i += step

        else:
            # ---- Your 10/30/50/70/90 checkpoint strategy ----
            i = cfg.start_id

            # 1) Check the starting point; if invalid, jump to next checkpoint.
            while True:
                if (cfg.start_id <= cfg.end_id and i > cfg.end_id) or (cfg.start_id > cfg.end_id and i < cfg.end_id):
                    break

                id_str = str(i)
                try:
                    vres = validate_only(s, site=site,id_solicitud=id_str,
                                         timeout_s=cfg.timeout_s, max_retries=cfg.max_retries)
                    processed += 1

                    if vres["valid"]:
                        # If valid and we're at a checkpoint OR not—either way,
                        # expand sequentially forward until the first invalid.
                        j = i
                        while True:
                            jid = str(j)
                            # For the first one we already validated; fetch data if requested
                            if j == i:
                                if cfg.fetch_data_for_valid:
                                    full = load_valid_id_full(s, site=site,id_solicitud=jid,
                                                              timeout_s=cfg.timeout_s, max_retries=cfg.max_retries)
                                    _notify(cfg.webhook_url, "item", {"job_id": job_id, "id": jid,
                                                                      "valid": True,"operator":cfg.operator, **full})
                                else:
                                    _notify(cfg.webhook_url, "item", {"job_id": job_id, "id": jid, "valid": True})
                                found += 1
                            else:
                                # Validate next sequential id
                                vnext = validate_only(s, site=site,id_solicitud=jid,
                                                      timeout_s=cfg.timeout_s, max_retries=cfg.max_retries)
                                processed += 1
                                if vnext["valid"]:
                                    if cfg.fetch_data_for_valid:
                                        full = load_valid_id_full(s, site=site,id_solicitud=jid,
                                                                  timeout_s=cfg.timeout_s, max_retries=cfg.max_retries)
                                        _notify(cfg.webhook_url, "item", {"job_id": job_id, "id": jid,
                                                                          "valid": True,"operator":cfg.operator, **full})
                                    else:
                                        _notify(cfg.webhook_url, "item", {"job_id": job_id, "id": jid, "valid": True})
                                    found += 1
                                else:
                                    #_notify(cfg.webhook_url, "item", {"job_id": job_id, "id": jid,
                                    #                                  "valid": False,
                                    #                                  "reason": "ValidaSolicitud returned false",
                                    #                                  "valida_raw": vnext["valida_raw"]})
                                    skipped += 1
                                    # Stop expansion on first invalid
                                    break

                            if cfg.delay_ms: time.sleep(cfg.delay_ms/1000.0)
                            j += 1
                            if (cfg.start_id <= cfg.end_id and j > cfg.end_id) or (cfg.start_id > cfg.end_id and j < cfg.end_id):
                                break

                        # Now jump to next checkpoint **after** the last tested ID
                        next_after = j if not vres["valid"] else j  # j already points at the first invalid or end+1
                        i = next_checkpoint_id(next_after)
                        continue

                    else:
                        # Starting point invalid → jump to next checkpoint
                        #_notify(cfg.webhook_url, "item", {"job_id": job_id, "id": id_str,
                        #                                  "valid": False, "reason": "ValidaSolicitud returned false",
                        #                                  "valida_raw": vres["valida_raw"]})
                        skipped += 1
                        i = next_checkpoint_id(i + 1)
                        if cfg.delay_ms: time.sleep(cfg.delay_ms/1000.0)
                        continue

                except Exception as e:
                    errors += 1
                    _notify(cfg.webhook_url, "item_error", {"job_id": job_id, "id": id_str, "error": str(e)})
                    i = next_checkpoint_id(i + 1)
                    if cfg.delay_ms: time.sleep(cfg.delay_ms/1000.0)
                    continue

                # Safety end condition
                if (cfg.start_id <= cfg.end_id and i > cfg.end_id) or (cfg.start_id > cfg.end_id and i < cfg.end_id):
                    break

    finally:
        _notify(cfg.webhook_url, "scan_finished", {
            "job_id": job_id,
            "stats": {"processed": processed, "found": found, "skipped": skipped, "errors": errors}
        })
        if s is not None:
            s.close()

# ---------- API ----------
@app.post("/scan/range")
def scan_range(body: ScanIn, bg: BackgroundTasks):
    # Basic reachability guard for linear; checkpoint ignores step and jumps itself
    if body.start_id == body.end_id and body.strategy == "linear":
        pass
    elif body.strategy == "linear":
        step = 1 if body.start_id <= body.end_id else -1
        if step > 0 and body.end_id < body.start_id:
            raise HTTPException(status_code=400, detail="end_id must be >= start_id for linear ascending")
        if step < 0 and body.end_id > body.start_id:
            raise HTTPException(status_code=400, detail="end_id must be <= start_id for linear descending")

    job_id = str(uuid.uuid4())
    bg.add_task(run_scan, job_id, body)
    return {"job_id": job_id, "status": "started"}

@app.get("/health")
def health():
    return {"ok": True}


JOBS = {}

class AuditEnqueueIn(BaseModel):
    ids: List[int]
    operator: Operator = "afinia"         # NEW
    webhook_url: HttpUrl
    timeout_s: int = Field(30, ge=5)
    max_retries: int = Field(2, ge=0)
    delay_ms: int = Field(25, ge=0)
    notify_invalid: bool = False

class AuditEnqueueOut(BaseModel):
    job_id: str
    accepted: bool = True

def _run_audit_job(job_id: str, cfg: AuditEnqueueIn):
    
 

    sess: Optional[requests.Session] = None
    sess = make_session_for_operator(cfg.operator,False)
    t0 = time.time()
    processed = found = skipped = errors = 0

    # started
    try:
        sess.post(cfg.webhook_url, json={
            "event": "audit_started", "job_id": job_id, "count": len(cfg.ids)
        }, timeout=10)
    except Exception:
        pass

    try:
        for the_id in cfg.ids:
            try:
                res = get_audit_for_id(
                    the_id,
                    operator=cfg.operator,  
                    timeout_s=cfg.timeout_s,
                    max_retries=cfg.max_retries,
                    webhook_url=None,      # prevent double posts
                    session=sess
                )
                processed += 1
                is_valid = bool(res.get("valid"))

                if is_valid:
                    found += 1
                else:
                    skipped += 1

                if cfg.webhook_url and (is_valid or cfg.notify_invalid):
                    try:
                        sess.post(cfg.webhook_url, json={
                            "event": "audit_item",
                            "job_id": job_id,
                            **res
                        }, timeout=15)
                    except Exception:
                        pass

            except Exception as e:
                errors += 1
                if cfg.webhook_url:
                    try:
                        sess.post(cfg.webhook_url, json={
                            "event": "item_error",
                            "job_id": job_id,
                            "id": str(the_id),
                            "error": str(e),
                        }, timeout=10)
                    except Exception:
                        pass

            if cfg.delay_ms:
                time.sleep(cfg.delay_ms / 1000.0)

    finally:
        duration = round(time.time() - t0, 3)
        if cfg.webhook_url:
            try:
                sess.post(cfg.webhook_url, json={
                    "event": "audit_finished",
                    "job_id": job_id,
                    "stats": {
                        "processed": processed,
                        "found": found,
                        "skipped": skipped,
                        "errors": errors,
                        "duration_s": duration
                    }
                }, timeout=10)
            except Exception:
                pass
        sess.close()
        JOBS[job_id] = {"done": True, "stats": {"processed": processed, "found": found, "skipped": skipped, "errors": errors, "duration_s": duration}}

@app.post("/audit/batch", response_model=AuditEnqueueOut)
def audit_enqueue(body: AuditEnqueueIn):
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"done": False}
    t = threading.Thread(target=_run_audit_job, args=(job_id, body), daemon=True)
    t.start()
    # immediate, small response so Apps Script never waits
    return AuditEnqueueOut(job_id=job_id, accepted=True)


#------------------------ Update --------------------------------

class StatusItem(BaseModel):
    id: int
    operator: Operator = "afinia"
    last_status_text: Optional[str] = None     # what your Sheet currently has (e.g., "Pendiente documento")
    last_status_code: Optional[int] = None     # optional, if you store numeric code too

class StatusRefreshIn(BaseModel):
    items: List[StatusItem]
    webhook_url: HttpUrl
    timeout_s: int = Field(30, ge=5)
    max_retries: int = Field(2, ge=0)
    delay_ms: int = Field(25, ge=0)
    # Optional: only include auditoría entries on/after this many days back
    days_back: Optional[int] = Field(None, ge=0, description="If set, only include audit entries in the last X days")

class StatusRefreshSummary(BaseModel):
    processed: int
    changed: int
    invalid: int
    errors: int
    duration_s: float

@app.post("/status/refresh", response_model=StatusRefreshSummary)
def status_refresh(body: StatusRefreshIn):
    """
    For each (id, operator, last_status_text/last_status_code), fetch current status.
    If it changed, also fetch auditoría (optionally filtered by days_back) and POST a status_change event.
    """
    t0 = time.time()
    processed = changed = invalid = errors = 0

    # We can mix operators in one run; create a session cache per operator
    sessions: Dict[str, requests.Session] = {}
    cutoff_ms: Optional[int] = None
    if body.days_back is not None:
        cutoff_ms = int((time.time() - body.days_back * 86400) * 1000)

    try:
        # notify start
        try:
            requests.post(body.webhook_url, json={
                "event": "status_refresh_started",
                "count": len(body.items),
                "days_back": body.days_back
            }, timeout=10)
        except Exception:
            pass

        for it in body.items:
            processed += 1
            try:
                op = it.operator
                sess = sessions.get(op)
                if sess is None:
                    sess = make_session_for_operator(op, insecure_verify=False)
                    sessions[op] = sess

                # 1) live status
                live = get_status_for_id(
                    it.id, operator=op,
                    timeout_s=body.timeout_s, max_retries=body.max_retries,
                    session=sess
                )
                if not live.get("valid"):
                    invalid += 1
                    continue

                live_code = live.get("status_code")
                live_text = (live.get("status_text") or "").strip() if live.get("status_text") else None
                old_code = it.last_status_code
                old_text = (it.last_status_text or "").strip() if it.last_status_text else None
                 
                # 2) did it change? (compare text if present; else compare code)
                changed_now = False
                if old_text is not None and live_text is not None:
                    changed_now = (live_text != old_text)
                elif old_code is not None and live_code is not None:
                    changed_now = (str(live_code) != str(old_code))
                else:
                    # If we only have one side, treat as changed to be safe
                    changed_now = True

                if changed_now:
                    changed += 1
                    # 3) load auditoría for context
                    form_url = live.get("referer_used")
                    if not form_url:
                        # prime page if needed
                        # (get_status_for_id already primed; but keep safe)
                        form_url = live.get("referer_used")

                    audit = load_auditoria(sess, site=get_site(op), form_url=form_url, timeout_s=body.timeout_s, max_retries=body.max_retries)
                    audit = filter_audit_since(audit, cutoff_ms)

                    # 4) notify webhook about change
                    try:
                        requests.post(body.webhook_url, json={
                            "event": "status_change",
                            "id": it.id,
                            "old_status_text": old_text,
                            "old_status_code": old_code,
                            "new_status_text": live_text,
                            "new_status_code": live_code,
                            "audit": audit or []
                        }, timeout=15)
                    except Exception:
                        print(str(e))

                # be gentle
                if body.delay_ms:
                    time.sleep(body.delay_ms / 1000.0)

            except Exception as e:
                errors += 1
                try:
                    requests.post(body.webhook_url, json={
                        "event": "status_item_error",
                        "id": it.id,
                        "operator": it.operator,
                        "error": str(e)
                    }, timeout=10)
                except Exception:
                    pass

    finally:
        for s in sessions.values():
            try: s.close()
            except: pass
        try:
            requests.post(body.webhook_url, json={
                "event": "status_refresh_finished",
                "stats": {
                    "processed": processed, "changed": changed, "invalid": invalid, "errors": errors,
                    "duration_s": round(time.time() - t0, 3)
                }
            }, timeout=10)
            
        except Exception:
            pass

    return StatusRefreshSummary(
        processed=processed, changed=changed, invalid=invalid, errors=errors,
        duration_s=round(time.time() - t0, 3)
    )