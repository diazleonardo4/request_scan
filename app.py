# app.py
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel, HttpUrl, Field
from typing import Optional, Any, Dict
import requests, urllib.parse, json, time, uuid

BASE = "https://servicios.energiacaribemar.co"

app = FastAPI(title="EnergiaCaribe Range Scanner")

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
def validate_only(s: requests.Session, *, id_solicitud: str,
                  timeout_s: int, max_retries: int) -> Dict[str, Any]:
    common = {
        "User-Agent": "Mozilla/5.0",
        "Origin": BASE,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    r1 = _post_json_with_retries(
        s,
        f"{BASE}/Autogeneracion/WFConsulta.aspx/ValidaSolicitud",
        json_body={"ID_SOLICITUD": id_solicitud, "EMAIL": ""},
        headers={**common, "Content-Type": "application/json; charset=UTF-8",
                 "Referer": f"{BASE}/Autogeneracion/"},
        timeout=timeout_s,
        retries=max_retries,
    )
    valida_res = _unwrap_d(r1.json())
    return {"valid": _boolish(valida_res), "valida_raw": valida_res}

# ---------- full load for valid IDs ----------
def load_valid_id_full(s: requests.Session, *, id_solicitud: str,
                       timeout_s: int, max_retries: int) -> Dict[str, Any]:
    common = {
        "User-Agent": "Mozilla/5.0",
        "Origin": BASE,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }

    # We already did ValidaSolicitud in the caller, but doing it again is cheap; skip here.

    # Encrypt
    r2 = _post_json_with_retries(
        s,
        f"{BASE}/Autogeneracion/WFConsulta.aspx/Encryptar",
        json_body={"strParameter": f"ID_SOLICITUD={id_solicitud}"},
        headers={**common, "Content-Type": "application/json; charset=UTF-8",
                 "Referer": f"{BASE}/Autogeneracion/"},
        timeout=timeout_s,
        retries=max_retries,
    )
    enc_qs_raw = _unwrap_d(r2.json())  # "?enc=..."
    if not isinstance(enc_qs_raw, str) or not enc_qs_raw.startswith("?enc="):
        return {"warning": "Encryptar unexpected payload", "encryptar_raw": enc_qs_raw}

    # GET form to prime page-level state
    enc_qs_encoded = urllib.parse.quote(enc_qs_raw, safe="=?&")
    form_url = f"{BASE}/Autogeneracion/form/WFSolicitud.aspx{enc_qs_encoded}"
    _get_with_retries(
        s, form_url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout_s, retries=max_retries
    )

    # Page method: CargarDatosSolicitud (force zero-length body)
    url_cargar = f"{BASE}/Autogeneracion/form/WFSolicitud.aspx/CargarDatosSolicitud"
    r4 = s.post(
        url_cargar,
        headers={**common, "Content-Type": "application/json; charset=utf-8",
                 "Referer": form_url},
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

    s = requests.Session()
    processed = found = skipped = errors = 0

    try:
        if cfg.strategy == "linear":
            # Fallback: simple linear scan (optional)
            i = cfg.start_id
            step = 1 if cfg.start_id <= cfg.end_id else -1
            while (i <= cfg.end_id) if step > 0 else (i >= cfg.end_id):
                id_str = str(i)
                try:
                    vres = validate_only(s, id_solicitud=id_str,
                                         timeout_s=cfg.timeout_s, max_retries=cfg.max_retries)
                    if vres["valid"] and cfg.fetch_data_for_valid:
                        full = load_valid_id_full(s, id_solicitud=id_str,
                                                  timeout_s=cfg.timeout_s, max_retries=cfg.max_retries)
                        _notify(cfg.webhook_url, "item", {"job_id": job_id, "id": id_str,
                                                          "valid": True, **full})
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
                    vres = validate_only(s, id_solicitud=id_str,
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
                                    full = load_valid_id_full(s, id_solicitud=jid,
                                                              timeout_s=cfg.timeout_s, max_retries=cfg.max_retries)
                                    _notify(cfg.webhook_url, "item", {"job_id": job_id, "id": jid,
                                                                      "valid": True, **full})
                                else:
                                    _notify(cfg.webhook_url, "item", {"job_id": job_id, "id": jid, "valid": True})
                                found += 1
                            else:
                                # Validate next sequential id
                                vnext = validate_only(s, id_solicitud=jid,
                                                      timeout_s=cfg.timeout_s, max_retries=cfg.max_retries)
                                processed += 1
                                if vnext["valid"]:
                                    if cfg.fetch_data_for_valid:
                                        full = load_valid_id_full(s, id_solicitud=jid,
                                                                  timeout_s=cfg.timeout_s, max_retries=cfg.max_retries)
                                        _notify(cfg.webhook_url, "item", {"job_id": job_id, "id": jid,
                                                                          "valid": True, **full})
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
