"""Execução em lote da migração Jira Service Management -> GLPI."""
from __future__ import annotations
import csv, json, os, subprocess, sys, time
from datetime import datetime
from pathlib import Path
from typing import Any
import requests
import unicodedata

ROOT = Path(__file__).resolve().parent
PILOT_SCRIPT = ROOT / "pilot_migration.py"
PILOT_DATA_DIR = ROOT / "pilot_data"
RESULTS_DIR = ROOT / "batch_results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Variável obrigatória ausente: {name}")
    return value

def get_int_env(name: str, default: int = 0) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} deve ser inteiro.") from exc

JIRA_BASE = require_env("JIRA_BASE_URL").rstrip("/")
JIRA_EMAIL = require_env("JIRA_EMAIL")
JIRA_TOKEN = require_env("JIRA_API_TOKEN")

DEFAULT_JQL = (
    'project = TAREFA '
    'AND statusCategory = Done '
    'ORDER BY key ASC'
)
BATCH_JQL = os.environ.get("BATCH_JQL", DEFAULT_JQL).strip()
DRY_RUN = os.environ.get("DRY_RUN", "1").strip() == "1"
DISCOVERY_ONLY = os.environ.get("BATCH_DISCOVERY_ONLY", "0").strip() == "1"
STOP_ON_ERROR = os.environ.get("BATCH_STOP_ON_ERROR", "0").strip() == "1"
BATCH_LIMIT = get_int_env("BATCH_LIMIT", 0)
EXCLUDE_KEYS = {k.strip().upper() for k in os.environ.get("BATCH_EXCLUDE_KEYS", "").split(",") if k.strip()}
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
CSV_REPORT = RESULTS_DIR / f"migration_{RUN_ID}.csv"
JSON_REPORT = RESULTS_DIR / f"migration_{RUN_ID}.json"
REPORT_FIELDS = [
    "jira_key","summary","jira_status","result","exit_code","glpi_ticket_id",
    "attachments","comments","skipped_comments","recovered_existing",
    "duration_seconds","timestamp",
]
CANCELLED_STATUSES = {
    "cancelado",
    "cancelar solicitacao",
}

BATCH_ONLY_NEW = (
    os.environ.get(
        "BATCH_ONLY_NEW",
        "1",
    ).strip()
    == "1"
)

def normalize_text(value: Any) -> str:
    text = " ".join(
        str(value or "")
        .strip()
        .casefold()
        .split()
    )

    normalized = unicodedata.normalize(
        "NFKD",
        text,
    )

    return "".join(
        char
        for char in normalized
        if not unicodedata.combining(char)
    )

def build_jira_session() -> requests.Session:
    s = requests.Session()
    s.auth = (JIRA_EMAIL, JIRA_TOKEN)
    s.headers.update({"Accept":"application/json","Content-Type":"application/json"})
    return s

def search_jira_issues(session: requests.Session) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        body: dict[str, Any] = {"jql":BATCH_JQL,"maxResults":100,"fields":["summary","status"]}
        if token:
            body["nextPageToken"] = token
        r = session.post(f"{JIRA_BASE}/rest/api/3/search/jql", json=body, timeout=60)
        if not r.ok:
            print("\nERRO NA CONSULTA JQL")
            print(f"HTTP: {r.status_code}")
            print(r.text[:3000])
            r.raise_for_status()
        data = r.json()
        issues.extend(data.get("issues") or [])
        token = data.get("nextPageToken")
        if not token:
            break
    return issues

def load_ticket_state(jira_key: str) -> dict[str, Any]:
    path = PILOT_DATA_DIR / jira_key / "state.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_reports(results: list[dict[str, Any]]) -> None:
    tjson = JSON_REPORT.with_suffix(".json.tmp")
    tjson.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    tjson.replace(JSON_REPORT)
    tcsv = CSV_REPORT.with_suffix(".csv.tmp")
    with tcsv.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REPORT_FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(results)
    tcsv.replace(CSV_REPORT)

def run_single_issue(issue: dict[str, Any]) -> dict[str, Any]:
    key = str(issue.get("key") or "")
    fields = issue.get("fields") or {}
    summary = fields.get("summary") or ""
    status = (fields.get("status") or {}).get("name") or ""
    print("\n" + "#"*78)
    print(f"PROCESSANDO {key}\n{summary}\nStatus Jira: {status}")
    print("#"*78 + "\n")
    env = os.environ.copy()
    env["JIRA_ISSUE_KEY"] = key
    env.pop("GLPI_TARGET_TICKET_ID", None)
    started = time.monotonic()
    proc = subprocess.run([sys.executable, str(PILOT_SCRIPT)], cwd=ROOT, env=env, check=False)
    duration = round(time.monotonic()-started, 2)
    state = load_ticket_state(key)
    return {
        "jira_key":key,
        "summary":summary,
        "jira_status":status,
        "result":"OK" if proc.returncode == 0 else "ERRO",
        "exit_code":proc.returncode,
        "glpi_ticket_id":state.get("glpi_ticket_id") or "",
        "attachments":len(state.get("uploaded_attachments") or {}),
        "comments":len(state.get("imported_comments") or {}),
        "skipped_comments":len(state.get("skipped_comments") or {}),
        "recovered_existing":bool(state.get("recovered_existing")),
        "duration_seconds":duration,
        "timestamp":datetime.now().astimezone().isoformat(timespec="seconds"),
    }

def main() -> int:
    if not PILOT_SCRIPT.exists():
        raise RuntimeError(f"Arquivo não encontrado: {PILOT_SCRIPT}")
    print("="*78); print("MIGRAÇÃO JIRA -> GLPI EM LOTE"); print("="*78)
    print("\nJQL:\n" + BATCH_JQL)
    print(f"\nDRY_RUN..............: {DRY_RUN}")
    print(f"DISCOVERY_ONLY.......: {DISCOVERY_ONLY}")
    print(f"BATCH_LIMIT..........: {BATCH_LIMIT or 'SEM LIMITE'}")
    print(f"BATCH_STOP_ON_ERROR..: {STOP_ON_ERROR}")
    if EXCLUDE_KEYS:
        print("EXCLUÍDOS............: " + ", ".join(sorted(EXCLUDE_KEYS)))
    if not DRY_RUN and os.environ.get("BATCH_CONFIRM", "").strip() != "MIGRAR":
        print('\nBLOQUEADO: DRY_RUN=0 exige $env:BATCH_CONFIRM="MIGRAR"')
        return 20
    s = build_jira_session()
    try:
        issues = search_jira_issues(s)
    finally:
        s.close()
    selected = [i for i in issues if str(i.get("key") or "").upper() not in EXCLUDE_KEYS]
    if BATCH_LIMIT > 0:
        selected = selected[:BATCH_LIMIT]
    print(f"\nChamados encontrados: {len(issues)}\nChamados selecionados: {len(selected)}\n")
    for n, issue in enumerate(selected, 1):
        fields = issue.get("fields") or {}; status = (fields.get("status") or {}).get("name") or ""
        print(f"{n:03d}. {issue.get('key')} | {status} | {fields.get('summary') or ''}")
    if DISCOVERY_ONLY:
        print("\nSomente descoberta.\nNenhuma migração executada.")
        return 0
    if not selected:
        print("Nenhum chamado elegível."); return 0
    results: list[dict[str, Any]] = []
    try:
        for n, issue in enumerate(selected, 1):
            print(f"\n[{n}/{len(selected)}]")
            result = run_single_issue(issue)
            results.append(result); save_reports(results)
            if result["result"] != "OK":
                print(f"\nERRO em {result['jira_key']}.")
                if STOP_ON_ERROR:
                    print("Lote interrompido por configuração."); break
    except KeyboardInterrupt:
        print("\nLote interrompido pelo usuário."); save_reports(results); return 130
    save_reports(results)
    ok = sum(1 for r in results if r["result"] == "OK")
    err = len(results)-ok
    recovered = sum(1 for r in results if r.get("recovered_existing"))
    print("\n" + "="*78); print("RESUMO FINAL"); print("="*78)
    print(f"Executados....: {len(results)}\nOK............: {ok}\nErros.........: {err}\nRecuperados...: {recovered}")
    print(f"CSV...........: {CSV_REPORT}\nJSON..........: {JSON_REPORT}")
    return 2 if err else 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrompido."); sys.exit(130)
    except Exception as exc:
        print(f"\nERRO FATAL: {exc}"); sys.exit(1)
