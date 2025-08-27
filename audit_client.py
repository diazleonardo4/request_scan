# audit_client.py
from __future__ import annotations
import json
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import requests


BASE = "https://servicios.energiacaribemar.co"
UA = "Mozilla/5.0"  # keep browser-y


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

def validate_only(
    s: requests.Session,
    *,
    id_solicitud: str,
    timeout_s: int,
    max_retries: int
) -> Dict[str, Any]:
    """Fast validity check (no data load)."""
    headers = {
        "User-Agent": UA,
        "Origin": BASE,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json; charset=UTF-8",
        "Referer": f"{BASE}/Autogeneracion/",
    }
    r = _post_json_with_retries(
        s,
        f"{BASE}/Autogeneracion/WFConsulta.aspx/ValidaSolicitud",
        json_body={"ID_SOLICITUD": id_solicitud, "EMAIL": ""},
        headers=headers,
        timeout=timeout_s,
        retries=max_retries,
    )
    raw = _unwrap_d(r.json())
    # Server returns various truthy/falsey forms; normalize to bool
    valid = False
    if isinstance(raw, bool):
        valid = raw
    elif isinstance(raw, str):
        valid = raw.strip().lower() in ("true", "1", "si", "sí", "ok", "yes", "y")
    elif raw is not None:
        valid = bool(raw)
    return {"valid": valid, "valida_raw": raw}


def encrypt_for_id(
    s: requests.Session,
    *,
    id_solicitud: str,
    timeout_s: int,
    max_retries: int
) -> str:
    headers = {
        "User-Agent": UA,
        "Origin": BASE,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json; charset=UTF-8",
        "Referer": f"{BASE}/Autogeneracion/",
    }
    r = _post_json_with_retries(
        s,
        f"{BASE}/Autogeneracion/WFConsulta.aspx/Encryptar",
        json_body={"strParameter": f"ID_SOLICITUD={id_solicitud}"},
        headers=headers,
        timeout=timeout_s,
        retries=max_retries,
    )
    enc_qs = _unwrap_d(r.json())
    if not isinstance(enc_qs, str) or not enc_qs.startswith("?enc="):
        raise RuntimeError(f"Encryptar returned unexpected payload: {enc_qs!r}")
    return enc_qs


def prime_form(
    s: requests.Session,
    *,
    enc_qs: str,
    timeout_s: int,
    max_retries: int
) -> str:
    """GET the WFSolicitud.aspx?enc=... to set page/session context. Returns the full form URL used."""
    enc_qs_encoded = urllib.parse.quote(enc_qs, safe="=?&")
    form_url = f"{BASE}/Autogeneracion/form/WFSolicitud.aspx{enc_qs_encoded}"
    _get_with_retries(s, form_url, headers={"User-Agent": UA}, timeout=timeout_s, retries=max_retries)
    return form_url


def load_auditoria(
    s: requests.Session,
    *,
    form_url: str,
    timeout_s: int,
    max_retries: int
) -> List[Dict[str, Any]]:
    """POST .../WFSolicitud.aspx/CargarDatosAuditoria with zero-length body; Referer must be the exact ?enc=... page."""
    url = form_url.rsplit("?", 1)[0] + "/CargarDatosAuditoria"
    headers = {
        "User-Agent": UA,
        "Origin": BASE,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json; charset=utf-8",
        "Referer": form_url,
    }
    r = s.post(url, headers=headers, data=b"", timeout=timeout_s)
    r.raise_for_status()
    data = _unwrap_d(r.json())
    if data is None:
        return []
    if isinstance(data, list):
        return data
    # Some pages return stringified JSON in "d"
    if isinstance(data, str):
        try:
            j = json.loads(data)
            return j if isinstance(j, list) else []
        except Exception:
            return []
    return []


# ------------------------- Public function -------------------------

def get_audit_for_id(
    id_solicitud: int | str,
    *,
    timeout_s: int = 30,
    max_retries: int = 2,
    webhook_url: Optional[str] = None,
    session: Optional[requests.Session] = None,
    emit_webhook=False
) -> Dict[str, Any]:
    """
    High-level convenience:
    - Validates ID
    - Encrypts & primes form
    - Calls CargarDatosAuditoria
    - Optionally POSTs the result to a webhook

    Returns:
      { "id": <int>, "valid": <bool>, "referer_used": <str|None>, "audit": <list>|None }
    """
    own_session = False
    if session is None:
        session = requests.Session()
        own_session = True

    try:
        id_str = str(id_solicitud)

        v = validate_only(session, id_solicitud=id_str, timeout_s=timeout_s, max_retries=max_retries)
        if not v["valid"]:
            if webhook_url and emit_webhook:
                _notify(webhook_url, "audit_item", {"id": id_str, "valid": False, "reason": "invalid_id"})
            return {"id": int(id_solicitud), "valid": False, "referer_used": None, "audit": None}

        enc_qs = encrypt_for_id(session, id_solicitud=id_str, timeout_s=timeout_s, max_retries=max_retries)
        form_url = prime_form(session, enc_qs=enc_qs, timeout_s=timeout_s, max_retries=max_retries)
        audit = load_auditoria(session, form_url=form_url, timeout_s=timeout_s, max_retries=max_retries)

        if webhook_url and emit_webhook:
            _notify(webhook_url, "audit_item", {
                "id": id_str, "valid": True, "referer_used": form_url, "audit": audit or []
            })

        return {"id": int(id_solicitud), "valid": True, "referer_used": form_url, "audit": audit or []}

    finally:
        if own_session:
            session.close()


# ------------------------- Optional: simple notifier -------------------------

def _notify(url: str, event: str, payload: Dict[str, Any]) -> None:
    try:
        requests.post(url, json={"event": event, **payload}, timeout=10)
    except Exception:
        # best-effort only
        pass


# ------------------------- CLI usage (optional) -------------------------

#if __name__ == "__main__":
#    import argparse, pprint
#    ap = argparse.ArgumentParser(description="Fetch CargarDatosAuditoria for a single ID")
#    ap.add_argument("id", type=int)
#    ap.add_argument("--webhook", type=str, default=None)
#    ap.add_argument("--timeout", type=int, default=30)
#    ap.add_argument("--retries", type=int, default=2)
#    args = ap.parse_args()
#
#    pp = pprint.PrettyPrinter()
#    res = get_audit_for_id(args.id, timeout_s=args.timeout, max_retries=args.retries, webhook_url=args.webhook)
#    pp.pprint(res)
