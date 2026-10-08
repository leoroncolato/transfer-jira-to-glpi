"""Reconstrói idempotência mínima após perda do pilot_data."""
from __future__ import annotations
import html, json, os, re, sys
from pathlib import Path
from typing import Any
import requests
from requests import Session

ROOT = Path(__file__).resolve().parent
PILOT_DATA_DIR = ROOT / "pilot_data"
PILOT_DATA_DIR.mkdir(parents=True, exist_ok=True)
REGISTRY_FILE = ROOT / "recovery_registry.json"
DUPLICATES_FILE = ROOT / "recovery_duplicates.json"
JIRA_KEY_RE = re.compile(r"\b(TAREFA-\d+)\b", re.IGNORECASE)
HEADER_RE = re.compile(r"Importado\s+do\s+Jira\s*:\s*(TAREFA-\d+)", re.IGNORECASE)

def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Variável obrigatória ausente: {name}")
    return value

GLPI_BASE = require_env("GLPI_BASE_URL").rstrip("/")
GLPI_REST_API = os.environ.get("GLPI_REST_API_BASE_URL", f"{GLPI_BASE}/apirest.php").rstrip("/")
GLPI_V1_API = os.environ.get("GLPI_V1_API_BASE_URL", f"{GLPI_BASE}/api.php/v1").rstrip("/")
GLPI_APP_TOKEN = require_env("GLPI_APP_TOKEN")
GLPI_USER_TOKEN = require_env("GLPI_USER_TOKEN")
RECOVERY_OVERWRITE = os.environ.get("RECOVERY_OVERWRITE", "0").strip() == "1"
PAGE_SIZE = int(os.environ.get("RECOVERY_PAGE_SIZE", "200"))

def init_glpi_session() -> Session:
    s = requests.Session()
    r = s.get(
        f"{GLPI_REST_API}/initSession",
        headers={
            "Authorization": f"user_token {GLPI_USER_TOKEN}",
            "App-Token": GLPI_APP_TOKEN,
            "Accept": "application/json",
        },
        timeout=30,
    )
    r.raise_for_status()
    token = r.json().get("session_token")
    if not token:
        raise RuntimeError("GLPI não retornou session_token.")
    s.headers.update({"Session-Token":token,"App-Token":GLPI_APP_TOKEN,"Accept":"application/json"})
    return s

def close_glpi_session(s: Session) -> None:
    try:
        s.get(f"{GLPI_REST_API}/killSession", timeout=30)
    finally:
        s.close()

def strip_html(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value or "")).split())

def extract_jira_key(ticket: dict[str, Any]) -> str | None:
    externalid = str(ticket.get("externalid") or "").strip()
    if externalid:
        m = JIRA_KEY_RE.search(externalid)
        if m:
            return m.group(1).upper()
    content = strip_html(str(ticket.get("content") or ""))
    m = HEADER_RE.search(content)
    return m.group(1).upper() if m else None

def get_all_tickets(s: Session) -> list[dict[str, Any]]:
    tickets: list[dict[str, Any]] = []
    start = 0
    while True:
        end = start + PAGE_SIZE - 1
        r = s.get(
            f"{GLPI_V1_API}/Ticket/",
            params={"range":f"{start}-{end}","get_hateoas":"false"},
            timeout=60,
        )
        if r.status_code not in {200,206}:
            print(f"HTTP {r.status_code}"); print(r.text[:3000]); r.raise_for_status()
        page = r.json()
        if not isinstance(page, list):
            raise RuntimeError("Resposta inesperada ao listar Tickets GLPI.")
        if not page:
            break
        tickets.extend(page)
        if len(page) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return tickets

def write_state(key: str, ticket_id: int) -> bool:
    folder = PILOT_DATA_DIR / key
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "state.json"
    if path.exists() and not RECOVERY_OVERWRITE:
        print(f"SKIP estado existente: {key}")
        return False
    state = {
        "jira_key":key,
        "glpi_ticket_id":ticket_id,
        "uploaded_attachments":{},
        "imported_comments":{},
        "skipped_comments":{},
        "recovered_existing":True,
        "recovery_note":"Estado reconstruído a partir do GLPI após perda do pilot_data. O migrador deve fazer SKIP integral por padrão para evitar duplicações.",
    }
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
    return True

def main() -> int:
    print("="*78); print("RECUPERAÇÃO DE ESTADO GLPI -> pilot_data"); print("="*78)
    s = init_glpi_session()
    try:
        tickets = get_all_tickets(s)
    finally:
        close_glpi_session(s)
    by_key: dict[str, list[int]] = {}
    for t in tickets:
        key = extract_jira_key(t)
        if key:
            by_key.setdefault(key, []).append(int(t["id"]))
    unique: dict[str,int] = {}
    duplicates: dict[str,list[int]] = {}
    for key, ids in sorted(by_key.items()):
        ids = sorted(set(ids))
        if len(ids) == 1:
            unique[key] = ids[0]
        else:
            duplicates[key] = ids
    created = 0
    for key, ticket_id in unique.items():
        if write_state(key, ticket_id):
            created += 1
            print(f"RECUPERADO: {key} -> GLPI {ticket_id}")
    registry = {
        "unique":unique,
        "duplicates":duplicates,
        "tickets_scanned":len(tickets),
        "jira_keys_found":len(by_key),
        "states_written":created,
    }
    REGISTRY_FILE.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    DUPLICATES_FILE.write_text(json.dumps(duplicates, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "="*78); print("RESUMO"); print("="*78)
    print(f"Tickets GLPI lidos........: {len(tickets)}")
    print(f"Jira Keys encontradas.....: {len(by_key)}")
    print(f"Estados únicos criados....: {created}")
    print(f"Keys duplicadas no GLPI...: {len(duplicates)}")
    print(f"Registro..................: {REGISTRY_FILE}")
    print(f"Duplicidades...............: {DUPLICATES_FILE}")
    if duplicates:
        print("\nATENÇÃO: nenhuma state foi criada para as Jira Keys duplicadas. Revise recovery_duplicates.json e escolha o Ticket GLPI correto antes de executar o lote.")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"\nERRO FATAL: {exc}"); sys.exit(1)
