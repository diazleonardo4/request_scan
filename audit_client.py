# audit_client.py
from __future__ import annotations
import json
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import requests, os, ssl
from typing import Literal, NamedTuple
Operator = Literal["afinia", "aire"]



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


# ------------------------- Low-level helpers -------------------------

def _unwrap_d(obj: Any) -> Any:
    """ASP.NET page methods often wrap payload in {"d": ...}. Also some return stringified JSON."""
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


def _post_json_with_retries(
    s: requests.Session,
    url: str,
    *,
    json_body: Any,
    headers: Dict[str, str],
    timeout: int,
    retries: int
) -> requests.Response:
    last: Optional[Exception] = None
    for _ in range(retries + 1):
        try:
            r = s.post(url, headers=headers, json=json_body, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(0.4)
    assert last is not None
    raise last


def _get_with_retries(
    s: requests.Session,
    url: str,
    *,
    headers: Dict[str, str],
    timeout: int,
    retries: int
) -> requests.Response:
    last: Optional[Exception] = None
    for _ in range(retries + 1):
        try:
            r = s.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(0.4)
    assert last is not None
    raise last


# ------------------------- Site-specific helpers -------------------------

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



def encrypt_for_id(s: requests.Session, *, site: Site, id_solicitud: str, timeout_s: int, max_retries: int) -> str:
    headers = {
        "User-Agent": site.ua,
        "Origin": site.base,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json; charset=UTF-8",
        "Referer": site.home_referer,
    }
    r = _post_json_with_retries(
        s, f"{site.base}{site.consulta_page}/Encryptar",
        json_body={"strParameter": f"ID_SOLICITUD={id_solicitud}"},
        headers=headers, timeout=timeout_s, retries=max_retries
    )
    enc_qs = _unwrap_d(r.json())
    if not (isinstance(enc_qs, str) and enc_qs.startswith("?enc=")):
        raise RuntimeError(f"Encryptar unexpected payload: {enc_qs!r}")
    return enc_qs



def prime_form(s: requests.Session, *, site: Site, enc_qs: str, timeout_s: int, max_retries: int) -> str:
    enc_qs_encoded = urllib.parse.quote(enc_qs, safe="=?&")
    form_url = f"{site.base}{site.form_page}{enc_qs_encoded}"
    _get_with_retries(s, form_url, headers={"User-Agent": site.ua}, timeout=timeout_s, retries=max_retries)
    return form_url



def load_auditoria(s: requests.Session, *, site: Site, form_url: str, timeout_s: int, max_retries: int):
    url = form_url.rsplit("?", 1)[0] + "/CargarDatosAuditoria"
    headers = {
        "User-Agent": site.ua,
        "Origin": site.base,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json; charset=utf-8",
        "Referer": form_url,
    }
    r = s.post(url, headers=headers, data=b"", timeout=timeout_s)
    r.raise_for_status()
    data = _unwrap_d(r.json())
    if data is None: return []
    if isinstance(data, list): return data
    if isinstance(data, str):
        try: 
            j = json.loads(data)
            return j if isinstance(j, list) else []
        except Exception:
            return []
    return []


# ------------------------- Public function -------------------------

def get_audit_for_id(id_solicitud: int | str, *, operator: Operator = "afinia",
                     timeout_s: int = 30, max_retries: int = 2,
                     webhook_url: Optional[str] = None, session: Optional[requests.Session] = None,
                     emit_webhook: bool = False) -> Dict[str, Any]:
    site = get_site(operator)
    own = False
    if session is None:
        session = requests.Session(); own = True
    try:
        id_str = str(id_solicitud)
        v = validate_only(session, site=site, id_solicitud=id_str, timeout_s=timeout_s, max_retries=max_retries)
        if not v["valid"]:
            if webhook_url and emit_webhook:
                _notify(webhook_url, "audit_item", {"id": id_str, "valid": False, "reason": "invalid_id"})
            return {"id": int(id_solicitud), "valid": False, "referer_used": None, "audit": None, "operator": operator}

        enc_qs = encrypt_for_id(session, site=site, id_solicitud=id_str, timeout_s=timeout_s, max_retries=max_retries)
        form_url = prime_form(session, site=site, enc_qs=enc_qs, timeout_s=timeout_s, max_retries=max_retries)
        audit = load_auditoria(session, site=site, form_url=form_url, timeout_s=timeout_s, max_retries=max_retries)

        if webhook_url and emit_webhook:
            _notify(webhook_url, "audit_item", {"id": id_str, "valid": True, "referer_used": form_url, "audit": audit or [], "operator": operator})

        return {"id": int(id_solicitud), "valid": True, "referer_used": form_url, "audit": audit or [], "operator": operator}
    finally:
        if own: session.close()



# ------------------------- Optional: simple notifier -------------------------

def _notify(url: str, event: str, payload: Dict[str, Any]) -> None:
    try:
        requests.post(url, json={"event": event, **payload}, timeout=10)
    except Exception:
        # best-effort only
        pass

#----------------------- Update Status------------------------------------


def cargar_datos_solicitud(sess: requests.Session, *, site, form_url: str, timeout_s: int, max_retries: int) -> Optional[Dict[str, Any]]:
    """
    Calls .../CargarDatosSolicitud and returns the first object (or None).
    """
    url = form_url.rsplit("?", 1)[0] + "/CargarDatosSolicitud"
    headers = {
        "User-Agent": site.ua,
        "Origin": site.base,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json; charset=utf-8",
        "Referer": form_url,
    }
    r = sess.post(url, headers=headers, data=b"", timeout=timeout_s)
    r.raise_for_status()
    data = _unwrap_d(r.json())
    if isinstance(data, list) and data:
        return data[0]
    return None

def get_status_for_id(
    id_solicitud: int | str,
    *,
    operator: str,
    timeout_s: int = 30,
    max_retries: int = 2,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """
    Returns {"id", "valid", "status_code", "status_text", "referer_used"}.
    If invalid, returns valid=False and no status.
    """
    site = get_site(operator)
    own = False
    if session is None:
        session = requests.Session()
        own = True
    try:
        id_str = str(id_solicitud)

        v = validate_only(session, site=site, id_solicitud=id_str, timeout_s=timeout_s, max_retries=max_retries)
        if not v["valid"]:
            return {"id": int(id_solicitud), "valid": False, "status_code": None, "status_text": None, "referer_used": None}

        enc_qs  = encrypt_for_id(session, site=site, id_solicitud=id_str, timeout_s=timeout_s, max_retries=max_retries)
        form_url = prime_form(session, site=site, enc_qs=enc_qs, timeout_s=timeout_s, max_retries=max_retries)

        datos = cargar_datos_solicitud(session, site=site, form_url=form_url, timeout_s=timeout_s, max_retries=max_retries)
        status_code = None
        status_text = None
        if datos:
            # Both fields exist in your earlier sample payload
            status_code = datos.get("ESTADO")
            status_text = datos.get("DESC_ESTADO")
        return {
            "id": int(id_solicitud),
            "valid": True,
            "status_code": status_code,
            "status_text": status_text,
            "referer_used": form_url,
        }
    finally:
        if own:
            session.close()

def filter_audit_since(audit_list, cutoff_ms: Optional[int]):
    """Filter auditoría entries by FECHA_AUDITORIA >= cutoff (ms). If cutoff_ms is None, returns original list."""
    if not audit_list or cutoff_ms is None:
        return audit_list or []
    out = []
    for a in audit_list:
        dt = a.get("FECHA_AUDITORIA")
        if isinstance(dt, str) and dt.startswith("/Date("):
            try:
                ms = int(dt[6:-2])
                if ms >= cutoff_ms:
                    out.append(a)
            except Exception:
                pass
    return out

