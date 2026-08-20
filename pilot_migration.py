import os
import sys
import json
from pathlib import Path

import requests


JIRA_BASE = os.environ["JIRA_BASE_URL"].rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_TOKEN = os.environ["JIRA_API_TOKEN"]
JIRA_KEY = os.environ["JIRA_ISSUE_KEY"]

GLPI_BASE = os.environ["GLPI_BASE_URL"].rstrip("/")
GLPI_APP_TOKEN = os.environ["GLPI_APP_TOKEN"]
GLPI_USER_TOKEN = os.environ["GLPI_USER_TOKEN"]
GLPI_ENTITY_ID = int(os.environ["GLPI_ENTITY_ID"])

DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"

ALLOWED_STATUSES = {
    "aguardando cliente",
    "em andamento",
    "itens pendentes",
}


def normalize_status(value):
    return " ".join(
        (value or "").strip().casefold().split()
    )

WORKDIR = Path("pilot_data") / JIRA_KEY
WORKDIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = WORKDIR / "state.json"
JIRA_JSON_FILE = WORKDIR / "jira_issue.json"


def adf_to_plain(node):
    """
    Fallback simples.
    Só será usado se Jira não retornar renderedFields.description.
    """
    if node is None:
        return ""

    if isinstance(node, list):
        return "".join(adf_to_plain(item) for item in node)

    if not isinstance(node, dict):
        return ""

    node_type = node.get("type")
    output = ""

    if node_type == "text":
        output += node.get("text", "")

    if node_type == "hardBreak":
        output += "\n"

    for child in node.get("content", []):
        output += adf_to_plain(child)

    if node_type in {
        "paragraph",
        "heading",
        "listItem",
        "blockquote",
    }:
        output += "\n"

    return output


def load_state():
    if not STATE_FILE.exists():
        return {
            "jira_key": JIRA_KEY,
            "glpi_ticket_id": None,
            "uploaded_attachments": [],
        }

    with STATE_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2,
        )


print("=" * 70)
print("1. LENDO CHAMADO DO JIRA")
print("=" * 70)

jira_url = f"{JIRA_BASE}/rest/api/3/issue/{JIRA_KEY}"

jira_response = requests.get(
    jira_url,
    auth=(JIRA_EMAIL, JIRA_TOKEN),
    headers={
        "Accept": "application/json",
    },
    params={
        "fields": "summary,description,attachment,status",
        "expand": "renderedFields",
    },
    timeout=60,
)

jira_response.raise_for_status()

issue = jira_response.json()

with JIRA_JSON_FILE.open("w", encoding="utf-8") as f:
    json.dump(
        issue,
        f,
        ensure_ascii=False,
        indent=2,
    )

summary = issue["fields"].get("summary") or ""
status = (issue["fields"].get("status") or {}).get("name") or ""

rendered_fields = issue.get("renderedFields") or {}
description_html = rendered_fields.get("description")

if not description_html:
    print(
        "AVISO: renderedFields.description veio vazio. "
        "Usando fallback textual do ADF."
    )

    description_html = adf_to_plain(
        issue["fields"].get("description")
    )

attachments = issue["fields"].get("attachment") or []

print(f"Jira........: {JIRA_KEY}")
print(f"Status......: {status}")
print(f"Título......: {summary}")
print(f"Anexos......: {len(attachments)}")
print(
    f"Descrição...: {len(description_html)} caracteres"
)

normalized_status = normalize_status(status)

if normalized_status not in ALLOWED_STATUSES:
    print()
    print(
        f"BLOQUEADO: status '{status}' "
        f"não pertence ao escopo."
    )
    sys.exit(10)


print()
print("=" * 70)
print("2. BAIXANDO ANEXOS DO JIRA")
print("=" * 70)

downloaded = []

for attachment in attachments:
    attachment_id = str(attachment["id"])
    filename = Path(attachment["filename"]).name
    mime_type = attachment.get(
        "mimeType",
        "application/octet-stream",
    )

    attachment_dir = WORKDIR / "attachments" / attachment_id
    attachment_dir.mkdir(parents=True, exist_ok=True)

    local_path = attachment_dir / filename

    print(
        f"Baixando Jira attachment "
        f"{attachment_id}: {filename}"
    )

    download_url = attachment.get("content")
    if not download_url:
        download_url = (
            f"{JIRA_BASE}/rest/api/3/"
            f"attachment/content/{attachment_id}"
        )

    response = requests.get(
        download_url,
        auth=(JIRA_EMAIL, JIRA_TOKEN),
        headers={
            "Accept": "*/*",
        },
        params={
            "redirect": "false",
        },
        stream=True,
        allow_redirects=True,
        timeout=120,
    )

    if not response.ok:
        print()
        print("ERRO AO BAIXAR ANEXO")
        print(f"HTTP........: {response.status_code}")
        print(
            "Content-Type:",
            response.headers.get("Content-Type")
        )
        print(f"URL.........: {response.url}")

        try:
            print(
                "Resposta.....:",
                response.text[:1000]
            )
        except Exception:
            pass

        response.raise_for_status()

    with local_path.open("wb") as f:
        for chunk in response.iter_content(
            chunk_size=1024 * 1024
        ):
            if chunk:
                f.write(chunk)

    downloaded.append(
        {
            "jira_attachment_id": attachment_id,
            "filename": filename,
            "mime_type": mime_type,
            "path": str(local_path),
            "size": local_path.stat().st_size,
        }
    )

    print(
        f"OK: {local_path.stat().st_size} bytes"
    )


print()
print("=" * 70)
print("3. RESUMO DO PILOTO")
print("=" * 70)

print(f"Título: {summary}")
print(f"Status: {status}")
print(f"Anexos baixados: {len(downloaded)}")

for item in downloaded:
    print(
        f"  - {item['filename']} "
        f"({item['size']} bytes)"
    )


if DRY_RUN:
    print()
    print("=" * 70)
    print("DRY-RUN CONCLUÍDO")
    print("Nenhum dado foi gravado no GLPI.")
    print("=" * 70)
    sys.exit(0)


print()
print("=" * 70)
print("4. AUTENTICANDO NO GLPI")
print("=" * 70)

glpi_init = requests.get(
    f"{GLPI_BASE}/apirest.php/initSession",
    headers={
        "Authorization": (
            f"user_token {GLPI_USER_TOKEN}"
        ),
        "App-Token": GLPI_APP_TOKEN,
        "Accept": "application/json",
    },
    timeout=30,
)

glpi_init.raise_for_status()

session_token = glpi_init.json()["session_token"]

glpi_headers = {
    "Session-Token": session_token,
    "App-Token": GLPI_APP_TOKEN,
    "Accept": "application/json",
}


state = load_state()


try:
    if state.get("glpi_ticket_id"):
        ticket_id = state["glpi_ticket_id"]

        print(
            f"Ticket GLPI já criado anteriormente: "
            f"{ticket_id}"
        )

    else:
        print()
        print("=" * 70)
        print("5. CRIANDO TICKET NO GLPI")
        print("=" * 70)

        ticket_payload = {
            "input": {
                "name": summary,
                "content": description_html,
                "entities_id": GLPI_ENTITY_ID,
            }
        }

        response = requests.post(
            f"{GLPI_BASE}/apirest.php/Ticket/",
            headers={
                **glpi_headers,
                "Content-Type": "application/json",
            },
            json=ticket_payload,
            timeout=60,
        )

        response.raise_for_status()

        result = response.json()

        ticket_id = result["id"]

        state["glpi_ticket_id"] = ticket_id
        save_state(state)

        print(
            f"Ticket GLPI criado com ID {ticket_id}"
        )


    print()
    print("=" * 70)
    print("6. TRANSFERINDO ANEXOS PARA O GLPI")
    print("=" * 70)

    uploaded_ids = set(
        state.get("uploaded_attachments", [])
    )

    for attachment in downloaded:
        jira_attachment_id = (
            attachment["jira_attachment_id"]
        )

        if jira_attachment_id in uploaded_ids:
            print(
                f"SKIP: Jira attachment "
                f"{jira_attachment_id} já transferido."
            )
            continue

        filename = attachment["filename"]
        local_path = Path(attachment["path"])
        mime_type = attachment["mime_type"]

        manifest = {
            "input": {
                "name": filename,
                "_filename": [filename],
            }
        }

        print(
            f"Enviando documento: {filename}"
        )

        with local_path.open("rb") as file_handle:
            files = {
                "uploadManifest": (
                    None,
                    json.dumps(
                        manifest,
                        ensure_ascii=False,
                    ),
                    "application/json",
                ),
                "filename[0]": (
                    filename,
                    file_handle,
                    mime_type,
                ),
            }

            upload_response = requests.post(
                f"{GLPI_BASE}/apirest.php/Document/",
                headers=glpi_headers,
                files=files,
                timeout=120,
            )

        upload_response.raise_for_status()

        upload_result = upload_response.json()
        document_id = upload_result["id"]

        print(
            f"Documento criado no GLPI: "
            f"{document_id}"
        )

        link_payload = {
            "input": {
                "documents_id": document_id,
                "itemtype": "Ticket",
                "items_id": ticket_id,
            }
        }

        link_response = requests.post(
            f"{GLPI_BASE}/apirest.php/Document_Item/",
            headers={
                **glpi_headers,
                "Content-Type": "application/json",
            },
            json=link_payload,
            timeout=60,
        )

        link_response.raise_for_status()

        state.setdefault(
            "uploaded_attachments",
            [],
        ).append(jira_attachment_id)

        save_state(state)

        print(
            f"Documento vinculado ao ticket "
            f"{ticket_id}"
        )


    print()
    print("=" * 70)
    print("PILOTO CONCLUÍDO")
    print("=" * 70)

    print(f"Jira: {JIRA_KEY}")
    print(f"GLPI: {ticket_id}")
    print(f"Título: {summary}")
    print(
        f"Anexos: "
        f"{len(state['uploaded_attachments'])}"
    )


finally:
    try:
        requests.get(
            f"{GLPI_BASE}/apirest.php/killSession",
            headers=glpi_headers,
            timeout=15,
        )
    except Exception:
        pass