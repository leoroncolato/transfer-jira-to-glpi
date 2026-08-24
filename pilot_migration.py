"""Migração piloto de chamados do Jira Service Management para o GLPI."""

from __future__ import annotations

import json
import os
import sys
import unicodedata
from html import escape
from pathlib import Path
from typing import Any
from datetime import datetime

import re
import requests
from requests import Response, Session


# =============================================================================
# CONFIGURAÇÃO
# =============================================================================


def require_env(name: str) -> str:
    """Retorna uma variável obrigatória ou encerra com erro claro."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Variável de ambiente obrigatória ausente: {name}")
    return value


def optional_int_env(name: str) -> int | None:
    """Lê um inteiro opcional do ambiente."""
    value = os.environ.get(name, "").strip()
    if not value:
        return None

    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} deve ser um número inteiro.") from exc


JIRA_BASE = require_env("JIRA_BASE_URL").rstrip("/")
JIRA_EMAIL = require_env("JIRA_EMAIL")
JIRA_TOKEN = require_env("JIRA_API_TOKEN")
JIRA_KEY = require_env("JIRA_ISSUE_KEY")
JIRA_SECTOR_FIELD_ID = "customfield_10154"
JIRA_BRANCH_FIELD_ID = "customfield_10119"

GLPI_BASE = require_env("GLPI_BASE_URL").rstrip("/")
GLPI_REST_API = os.environ.get(
    "GLPI_REST_API_BASE_URL",
    f"{GLPI_BASE}/apirest.php",
).rstrip("/")
GLPI_V1_API = os.environ.get(
    "GLPI_V1_API_BASE_URL",
    f"{GLPI_BASE}/api.php/v1",
).rstrip("/")
GLPI_APP_TOKEN = require_env("GLPI_APP_TOKEN")
GLPI_USER_TOKEN = require_env("GLPI_USER_TOKEN")
GLPI_ENTITY_ID = int(require_env("GLPI_ENTITY_ID"))
GLPI_JIRA_USER_ID = int(require_env("GLPI_JIRA_USER_ID"))
GLPI_TARGET_TICKET_ID = optional_int_env("GLPI_TARGET_TICKET_ID")

DRY_RUN = os.environ.get("DRY_RUN", "1").strip() == "1"

REQUEST_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 120
SESSION_TIMEOUT = 30

ALLOWED_STATUSES = {
    "aguardando cliente",
    "em andamento",
    "itens pendentes",
    "resolvido",
    "fechado",
    "concluida"
}

GLPI_PRIORITY_MAP = {
    "baixa": 2,
    "baixo": 2,
    "media": 3,
    "medio": 3,
    "alta": 4,
    "alto": 4,
    "muito alta": 5,
    "muito alto": 5,
}

GLPI_STATUS_MAP = {
    "novo": 1,
    "aberto": 1,

    "em andamento": 2,
    "aguardando pelo suporte": 2,

    "aguardando cliente": 4,
    "itens pendentes": 4,
    "pendente": 4,

    "resolvido": 5,
    "solucionado": 5,
    "concluida": 5,

    "fechado": 6,
    "encerrado": 6,
    "cancelado": 6,
    "cancelar solicitacao": 6,
}

GLPI_STATUS_LABELS = {
    1: "Novo",
    2: "Em atendimento (atribuído)",
    3: "Em atendimento (planejado)",
    4: "Pendente",
    5: "Solucionado",
    6: "Fechado",
}

EMAIL_QUOTE_PATTERNS = [
    re.compile(
        r"^\s*Em\s+\d{1,2}/\d{1,2}/\d{4}\s+.+?\s+escreveu:\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*On\s+.+?\s+wrote:\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*From:\s+",
        re.IGNORECASE,
    ),
]

WORKDIR = Path("pilot_data") / JIRA_KEY
ATTACHMENTS_DIR = WORKDIR / "attachments"
STATE_FILE = WORKDIR / "state.json"
JIRA_JSON_FILE = WORKDIR / "jira_issue.json"
JIRA_COMMENTS_FILE = WORKDIR / "jira_comments.json"

WORKDIR.mkdir(parents=True, exist_ok=True)
ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)



# =============================================================================
# UTILITÁRIOS
# =============================================================================


def section(title: str) -> None:
    """Imprime um separador de etapa."""
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def normalize_text(value: Any) -> str:
    """Normaliza texto para comparação previsível."""
    text = " ".join(str(value or "").strip().casefold().split())
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def response_preview(response: Response, limit: int = 3000) -> str:
    """Retorna uma amostra segura da resposta HTTP."""
    try:
        return response.text[:limit]
    except Exception:
        return "<resposta indisponível>"


def check_response(response: Response, operation: str, source: str) -> None:
    """Valida uma resposta HTTP e exibe contexto objetivo em caso de erro."""
    if response.ok:
        return

    section(f"ERRO {source}: {operation}")
    print(f"HTTP........: {response.status_code}")
    print(f"URL.........: {response.url}")
    print(f"Content-Type: {response.headers.get('Content-Type')}")
    print("Resposta:")
    print(response_preview(response))
    response.raise_for_status()


def save_json(path: Path, data: Any) -> None:
    """Grava JSON de forma atômica."""
    temp_path = path.with_suffix(path.suffix + ".tmp")

    with temp_path.open("w", encoding="utf-8") as file_handle:
        json.dump(data, file_handle, ensure_ascii=False, indent=2)

    temp_path.replace(path)


def adf_to_plain(node: Any) -> str:
    """Converte ADF do Jira em texto simples para fallback."""
    if node is None:
        return ""

    if isinstance(node, list):
        return "".join(adf_to_plain(item) for item in node)

    if not isinstance(node, dict):
        return str(node)

    node_type = node.get("type")
    output = ""

    if node_type == "text":
        output += str(node.get("text", ""))
    elif node_type == "hardBreak":
        output += "\n"

    for child in node.get("content", []):
        output += adf_to_plain(child)

    if node_type in {"paragraph", "heading", "listItem", "blockquote"}:
        output += "\n"

    return output

def resolve_glpi_status(jira_status_name: str) -> int:
    """Converte o status Jira para o status equivalente do GLPI."""
    status_key = normalize_text(jira_status_name)

    if status_key not in GLPI_STATUS_MAP:
        raise RuntimeError(
            f"Status Jira sem mapeamento para o GLPI: "
            f"'{jira_status_name}'."
        )

    return GLPI_STATUS_MAP[status_key]

def format_jira_datetime(value: str) -> str:
    """Converte datetime ISO do Jira para formato brasileiro."""
    if not value:
        return "Não informado"

    try:
        dt = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        return dt.strftime("%d/%m/%Y %H:%M:%S")

    except ValueError:
        return value


# =============================================================================
# ESTADO LOCAL / IDEMPOTÊNCIA
# =============================================================================


def default_state() -> dict[str, Any]:
    """Cria o estado inicial da migração."""
    return {
        "jira_key": JIRA_KEY,
        "glpi_ticket_id": GLPI_TARGET_TICKET_ID,
        "uploaded_attachments": {},
        "imported_comments": {},
        "skipped_comments": {},
    }


def load_state() -> dict[str, Any]:
    """Carrega o estado e aplica defaults compatíveis."""
    if not STATE_FILE.exists():
        return default_state()

    with STATE_FILE.open("r", encoding="utf-8") as file_handle:
        state = json.load(file_handle)

    if state.get("jira_key") not in {None, JIRA_KEY}:
        raise RuntimeError(
            f"Estado incompatível: esperado Jira {JIRA_KEY}, "
            f"encontrado {state.get('jira_key')}."
        )

    state["jira_key"] = JIRA_KEY
    state.setdefault("uploaded_attachments", {})
    state.setdefault("imported_comments", {})
    state.setdefault("glpi_ticket_id", None)
    state.setdefault("skipped_comments", {})

    if GLPI_TARGET_TICKET_ID is not None:
        state["glpi_ticket_id"] = GLPI_TARGET_TICKET_ID

    return state


def save_state(state: dict[str, Any]) -> None:
    """Persiste o estado da migração."""
    save_json(STATE_FILE, state)


# =============================================================================
# JIRA
# =============================================================================


def build_jira_session() -> Session:
    """Cria uma sessão autenticada para o Jira."""
    session = requests.Session()
    session.auth = (JIRA_EMAIL, JIRA_TOKEN)
    session.headers.update({"Accept": "application/json"})
    return session


def jira_get_json(
    session: Session,
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Executa GET JSON no Jira."""
    response = session.get(
        f"{JIRA_BASE}{path}",
        params=params,
        timeout=REQUEST_TIMEOUT,
    )
    check_response(response, path, "JIRA")
    return response.json()


def get_jira_issue(session: Session) -> dict[str, Any]:
    """Lê os campos principais e metadados internos do chamado."""

    requested_fields = [
        "summary",
        "description",
        "attachment",
        "status",
        "priority",
        "created",
        JIRA_SECTOR_FIELD_ID,
        JIRA_BRANCH_FIELD_ID,
    ]

    issue = jira_get_json(
        session,
        f"/rest/api/3/issue/{JIRA_KEY}",
        params={
            "fields": ",".join(requested_fields),
            "expand": "renderedFields",
        },
    )

    save_json(JIRA_JSON_FILE, issue)
    return issue



def request_field_value_to_text(value: Any) -> str:
    """
    Converte valores de campos do Jira Service Management
    para uma representação textual simples.
    """
    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    if isinstance(value, (int, float, bool)):
        return str(value)

    if isinstance(value, list):
        values = [
            request_field_value_to_text(item)
            for item in value
        ]

        return ", ".join(
            item for item in values if item
        )

    if isinstance(value, dict):
        # Campos de lista/dropdown do Jira costumam vir
        # em um desses formatos.
        for key in (
            "value",
            "name",
            "label",
            "displayName",
        ):
            candidate = value.get(key)

            if candidate not in (None, ""):
                return request_field_value_to_text(
                    candidate
                )

        # Fallback seguro para algum tipo de campo
        # ainda não encontrado durante os testes.
        return json.dumps(
            value,
            ensure_ascii=False,
        )

    return str(value)


def get_request_field(
    request_data: dict[str, Any],
    field_name: str,
) -> str:
    """
    Localiza um campo da solicitação pelo label,
    sem depender de customfield_xxxxx.
    """
    target = normalize_text(field_name)

    for field in (
        request_data.get("requestFieldValues") or []
    ):
        label = normalize_text(
            field.get("label") or ""
        )

        if label == target:
            return request_field_value_to_text(
                field.get("value")
            )

    return ""

def get_jira_request_info(session: Session) -> dict[str, str]:
    """Lê tipo da solicitação e requerente no Jira Service Management."""
    request_data = jira_get_json(
        session,
        f"/rest/servicedeskapi/request/{JIRA_KEY}",
    )
    sector = get_request_field(
        request_data,
        "Setor",
    )

    branch = get_request_field(
        request_data,
        "Filial",
    )

    service_desk_id = str(request_data["serviceDeskId"])
    request_type_id = str(request_data["requestTypeId"])

    request_type = jira_get_json(
        session,
        (
            f"/rest/servicedeskapi/servicedesk/{service_desk_id}"
            f"/requesttype/{request_type_id}"
        ),
    )

    reporter = request_data.get("reporter") or {}

    return {
        "service_desk_id": service_desk_id,
        "request_type_id": request_type_id,
        "request_type_name": request_type.get("name") or "",
        "reporter_account_id": reporter.get("accountId") or "",
        "reporter_name": reporter.get("displayName") or "",
        "reporter_email": reporter.get("emailAddress") or "",
        "sector": sector,
        "branch": branch,
    }


def get_jira_comments(session: Session) -> list[dict[str, Any]]:
    """Lê todos os comentários do chamado com paginação."""
    comments: list[dict[str, Any]] = []
    start = 0
    limit = 100

    while True:
        page = jira_get_json(
            session,
            f"/rest/servicedeskapi/request/{JIRA_KEY}/comment",
            params={
                "start": start,
                "limit": limit,
                "expand": "renderedBody",
            },
        )

        values = page.get("values") or []
        comments.extend(values)

        if page.get("isLastPage", True) or not values:
            break

        start += len(values)

    return comments


def get_jira_description_html(issue: dict[str, Any]) -> str:
    """Obtém descrição renderizada; usa ADF como fallback."""
    rendered_fields = issue.get("renderedFields") or {}
    rendered_description = rendered_fields.get("description")

    if isinstance(rendered_description, str) and rendered_description.strip():
        return rendered_description

    print("AVISO: descrição renderizada vazia; usando fallback ADF.")
    return escape(adf_to_plain(issue.get("fields", {}).get("description"))).replace(
        "\n", "<br>"
    )


def remove_quoted_email(text: str) -> str:
    """
    Remove a mensagem de e-mail citada abaixo da resposta nova.

    Exemplo:
        Não tinha nenhum item para aprovar.
        ...
        Em 18/08/2026 15:19, Fulano escreveu:
        [notificação inteira do Jira]

    Mantém somente a parte anterior a 'Em ... escreveu:'.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    for index, line in enumerate(lines):
        stripped = line.strip()

        if any(pattern.match(stripped) for pattern in EMAIL_QUOTE_PATTERNS):
            previous = "\n".join(lines[:index]).strip()

            if previous:
                return previous

    return "\n".join(lines).strip()


def clean_jira_wiki_markup(text: str) -> str:
    """
    Remove artefatos do Jira/e-mail que não fazem sentido no GLPI.
    Não remove o texto efetivamente escrito pelo usuário.
    """

    # Imagens referenciadas no comentário.
    # Os arquivos já são migrados separadamente como Documents.
    text = re.sub(
        r"![^!\r\n]+?\|thumbnail!",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Também cobre !arquivo.png! sem |thumbnail.
    text = re.sub(
        r"![^!\r\n]+\.(?:png|jpe?g|gif|webp)!",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Macros Jira legadas.
    text = re.sub(
        r"\{color(?::[^}]+)?\}",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"\{/?quote\}",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"\{/?noformat\}",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Marcadores produzidos pelo parser do e-mail do Jira.
    text = re.sub(
        r"<\\?!--.*?-->",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Links wiki: [Texto|https://...]
    # Mantemos texto + URL quando for um link legítimo.
    text = re.sub(
        r"\[([^\]|]+)\|([^\]]+)\]",
        r"\1 (\2)",
        text,
    )

    # Negrito Jira simples: *texto*
    text = re.sub(
        r"\*([^*\n]+)\*",
        r"\1",
        text,
    )

    # Linhas separadoras isoladas.
    text = re.sub(
        r"(?m)^\s*-{3,}\s*$",
        "",
        text,
    )

    # Excesso de linhas vazias.
    text = re.sub(
        r"\n[ \t]*\n(?:[ \t]*\n)+",
        "\n\n",
        text,
    )

    return text.strip()


def jira_comment_body_to_html(comment: dict[str, Any]) -> str:
    """
    Converte o BODY original do comentário Jira para HTML seguro
    e simples para o GLPI.
    """

    body = comment.get("body") or ""

    if isinstance(body, (dict, list)):
        body = adf_to_plain(body)

    text = str(body)

    # 1. remove corrente anterior do e-mail
    text = remove_quoted_email(text)

    # 2. remove macros Jira e referências redundantes
    text = clean_jira_wiki_markup(text)

    # 3. converte para HTML mínimo e seguro
    return escape(text).replace("\n", "<br>")


def build_glpi_description(
    description_html: str,
    reporter_name: str,
    reporter_email: str,
    jira_created: str,
    sector: str,
    branch: str,
) -> str:
    """Monta o cabeçalho histórico do Jira no GLPI."""

    reporter_display = (
        reporter_name or "Não informado"
    )

    if reporter_email:
        reporter_display += (
            f" — {reporter_email}"
        )

    sector_display = (
        sector or "Não informado"
    )

    branch_display = (
        branch or "Não informado"
    )

    return (
        "<p>"
        f"<strong>Importado do Jira:</strong> "
        f"{escape(JIRA_KEY)}<br>"

        f"<strong>Data de abertura no Jira:</strong> "
        f"{escape(jira_created)}<br>"

        f"<strong>Requerente original:</strong> "
        f"{escape(reporter_display)}<br>"

        f"<strong>Setor:</strong> "
        f"{escape(sector_display)}<br>"

        f"<strong>Filial:</strong> "
        f"{escape(branch_display)}"
        "</p>"

        "<hr>"

        f"{description_html}"
    )


def validate_jira_status(status: str) -> None:
    """Bloqueia chamados fora do escopo do piloto."""
    if normalize_text(status) not in ALLOWED_STATUSES:
        raise RuntimeError(f"Status fora do escopo do piloto: '{status}'.")


def download_jira_attachments(
    session: Session,
    attachments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Baixa anexos do Jira para o diretório local do chamado."""
    downloaded: list[dict[str, Any]] = []

    for attachment in attachments:
        attachment_id = str(attachment["id"])
        filename = Path(str(attachment.get("filename") or attachment_id)).name
        mime_type = attachment.get("mimeType") or "application/octet-stream"

        attachment_dir = ATTACHMENTS_DIR / attachment_id
        attachment_dir.mkdir(parents=True, exist_ok=True)
        local_path = attachment_dir / filename

        print(f"Baixando Jira attachment {attachment_id}: {filename}")

        response = session.get(
            f"{JIRA_BASE}/rest/api/3/attachment/content/{attachment_id}",
            headers={"Accept": "*/*"},
            stream=True,
            allow_redirects=True,
            timeout=DOWNLOAD_TIMEOUT,
        )
        check_response(response, f"download do anexo {attachment_id}", "JIRA")

        with local_path.open("wb") as file_handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file_handle.write(chunk)

        size = local_path.stat().st_size
        downloaded.append(
            {
                "jira_attachment_id": attachment_id,
                "filename": filename,
                "mime_type": mime_type,
                "path": str(local_path),
                "size": size,
            }
        )

        print(f"OK: {size} bytes")

    return downloaded


# =============================================================================
# GLPI
# =============================================================================


def init_glpi_session() -> Session:
    """Abre uma sessão autenticada no GLPI Legacy API."""
    session = requests.Session()

    response = session.get(
        f"{GLPI_REST_API}/initSession",
        headers={
            "Authorization": f"user_token {GLPI_USER_TOKEN}",
            "App-Token": GLPI_APP_TOKEN,
            "Accept": "application/json",
        },
        timeout=SESSION_TIMEOUT,
    )
    check_response(response, "initSession", "GLPI")

    session_token = response.json().get("session_token")
    if not session_token:
        raise RuntimeError("GLPI não retornou session_token.")

    session.headers.update(
        {
            "Session-Token": session_token,
            "App-Token": GLPI_APP_TOKEN,
            "Accept": "application/json",
        }
    )

    return session


def close_glpi_session(session: Session) -> None:
    """Encerra a sessão do GLPI sem mascarar o erro principal."""
    try:
        session.get(f"{GLPI_REST_API}/killSession", timeout=SESSION_TIMEOUT)
    except requests.RequestException:
        pass
    finally:
        session.close()


def glpi_request(
    session: Session,
    method: str,
    path: str,
    operation: str,
    *,
    api_base: str = GLPI_V1_API,
    **kwargs: Any,
) -> Response:
    """Executa uma operação em uma das bases da API do GLPI."""
    response = session.request(
        method,
        f"{api_base}/{path.lstrip('/')}",
        **kwargs,
    )
    check_response(response, operation, "GLPI")
    return response


def resolve_glpi_category_id(session: Session, category_name: str) -> int:
    """Resolve a categoria GLPI pelo nome do tipo de solicitação Jira."""
    response = glpi_request(
        session,
        "GET",
        "ITILCategory/",
        "consulta das categorias ITIL",
        params={"range": "0-999", "get_hateoas": "false"},
        timeout=REQUEST_TIMEOUT,
    )

    categories = response.json()
    target = normalize_text(category_name)

    matches = [
        category
        for category in categories
        if normalize_text(category.get("name")) == target
    ]

    if not matches:
        raise RuntimeError(f"Categoria GLPI não encontrada: '{category_name}'.")

    if len(matches) > 1:
        ids = [item.get("id") for item in matches]
        raise RuntimeError(
            f"Categoria GLPI ambígua para '{category_name}'. IDs: {ids}"
        )

    return int(matches[0]["id"])


def resolve_glpi_priority(jira_priority_name: str) -> int:
    """Converte prioridade Jira para a escala do GLPI."""
    priority_key = normalize_text(jira_priority_name)

    if priority_key not in GLPI_PRIORITY_MAP:
        raise RuntimeError(
            f"Prioridade Jira não mapeada: '{jira_priority_name}'."
        )

    return GLPI_PRIORITY_MAP[priority_key]


def sync_glpi_ticket(
    session: Session,
    state: dict[str, Any],
    *,
    summary: str,
    content: str,
    category_id: int,
    priority: int,
    status_id: int,
) -> int:
    """Cria o ticket ou atualiza o ticket já registrado no estado."""
    ticket_id = state.get("glpi_ticket_id")

    ticket_input = {
        "name": summary,
        "content": content,
        "entities_id": GLPI_ENTITY_ID,
        "priority": priority,
        "itilcategories_id": category_id,
        "status": status_id,
        "externalid": JIRA_KEY,
    }

    if ticket_id:
        ticket_id = int(ticket_id)
        payload = {"input": {"id": ticket_id, **ticket_input}}

        glpi_request(
            session,
            "PUT",
            f"Ticket/{ticket_id}",
            f"atualização do ticket {ticket_id}",
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )

        print(f"Ticket GLPI atualizado: {ticket_id}")
        return ticket_id

    response = glpi_request(
        session,
        "POST",
        "Ticket/",
        "criação do ticket",
        api_base=GLPI_REST_API,
        json={"input": ticket_input},
        timeout=REQUEST_TIMEOUT,
    )

    ticket_id = int(response.json()["id"])
    state["glpi_ticket_id"] = ticket_id
    save_state(state)

    print(f"Ticket GLPI criado: {ticket_id}")
    return ticket_id


def ensure_glpi_requester(session: Session, ticket_id: int) -> None:
    """Garante o usuário técnico do Jira como requerente do ticket."""
    response = glpi_request(
        session,
        "GET",
        f"Ticket/{ticket_id}/Ticket_User",
        "consulta dos atores do ticket",
        params={"range": "0-999", "get_hateoas": "false"},
        timeout=REQUEST_TIMEOUT,
    )

    actors = response.json()

    already_exists = any(
        int(actor.get("users_id") or 0) == GLPI_JIRA_USER_ID
        and int(actor.get("type") or 0) == 1
        for actor in actors
    )

    if already_exists:
        print(f"Requerente GLPI {GLPI_JIRA_USER_ID}: já associado.")
        return

    glpi_request(
        session,
        "POST",
        "Ticket_User/",
        "inclusão do requerente Jira",
        json={
            "input": {
                "tickets_id": ticket_id,
                "users_id": GLPI_JIRA_USER_ID,
                "type": 1,
            }
        },
        timeout=REQUEST_TIMEOUT,
    )

    print(f"Requerente GLPI {GLPI_JIRA_USER_ID}: associado.")


def transfer_attachments_to_glpi(
    session: Session,
    ticket_id: int,
    downloaded: list[dict[str, Any]],
    state: dict[str, Any],
) -> None:
    """Cria documentos no GLPI e vincula ao ticket sem repetir anexos concluídos."""
    uploaded = state.setdefault("uploaded_attachments", {})

    for attachment in downloaded:
        jira_attachment_id = str(attachment["jira_attachment_id"])

        if jira_attachment_id in uploaded:
            print(
                f"SKIP anexo Jira {jira_attachment_id}: "
                f"GLPI Document {uploaded[jira_attachment_id]}."
            )
            continue

        filename = str(attachment["filename"])
        local_path = Path(str(attachment["path"]))
        mime_type = str(attachment["mime_type"])

        manifest = {
            "input": {
                "name": filename,
                "entities_id": GLPI_ENTITY_ID,
                "_filename": [filename],
            }
        }

        print(f"Enviando documento: {filename}")

        with local_path.open("rb") as file_handle:
            files = {
                "uploadManifest": (
                    None,
                    json.dumps(manifest, ensure_ascii=False),
                    "application/json",
                ),
                "filename[0]": (filename, file_handle, mime_type),
            }

            upload_response = glpi_request(
                session,
                "POST",
                "Document/",
                f"upload do documento {filename}",
                api_base=GLPI_REST_API,
                files=files,
                timeout=DOWNLOAD_TIMEOUT,
            )

        document_id = int(upload_response.json()["id"])

        glpi_request(
            session,
            "POST",
            "Document_Item/",
            f"vínculo do documento {filename}",
            api_base=GLPI_REST_API,
            json={
                "input": {
                    "documents_id": document_id,
                    "itemtype": "Ticket",
                    "items_id": ticket_id,
                }
            },
            timeout=REQUEST_TIMEOUT,
        )

        uploaded[jira_attachment_id] = document_id
        save_state(state)

        print(f"Documento {document_id} vinculado ao ticket {ticket_id}.")

def enforce_glpi_ticket_status(
    session: Session,
    ticket_id: int,
    status_id: int,
) -> None:
    """Garante o status final esperado após toda a migração."""

    glpi_request(
        session,
        "PUT",
        f"Ticket/{ticket_id}",
        f"ajuste final do status do ticket {ticket_id}",
        json={
            "input": {
                "id": ticket_id,
                "status": status_id,
            }
        },
        timeout=REQUEST_TIMEOUT,
    )

    print(
        f"Status final.: GLPI {status_id} "
        f"({GLPI_STATUS_LABELS[status_id]})"
    )


def import_jira_comments(
    session: Session,
    ticket_id: int,
    comments: list[dict[str, Any]],
    state: dict[str, Any],
) -> None:
    """Importa comentários do Jira como acompanhamentos no GLPI."""
    imported = state.setdefault("imported_comments", {})
    skipped = state.setdefault("skipped_comments", {})

    for comment in comments:
        jira_comment_id = str(comment["id"])

        if jira_comment_id in imported:
            print(
                f"SKIP comentário Jira {jira_comment_id}: "
                f"GLPI Followup {imported[jira_comment_id]}."
            )
            continue

        if jira_comment_id in skipped:
            print(
                f"SKIP comentário Jira {jira_comment_id}: "
                f"{skipped[jira_comment_id]}."
            )
            continue

        is_public = bool(comment.get("public"))
        author = comment.get("author") or {}
        author_name = (
            author.get("displayName")
            or author.get("emailAddress")
            or "Não informado"
        )

        created = comment.get("created") or {}
        if isinstance(created, dict):
            original_date = (
                created.get("iso8601")
                or created.get("jira")
                or created.get("friendly")
                or "Não informada"
            )
        else:
            original_date = str(created)

        visibility = "Público" if is_public else "Interno / privado"
        original_body = jira_comment_body_to_html(comment)
        if not original_body.strip():
            skipped[jira_comment_id] = "vazio após limpeza / somente anexos"
            save_state(state)

            print(
                f"SKIP comentário Jira {jira_comment_id}: "
                f"somente anexos ou conteúdo vazio após limpeza."
            )
            continue

        content = (
            "<p>"
            "<strong>Importado do Jira</strong><br>"
            f"<strong>Autor original:</strong> {escape(str(author_name))}<br>"
            f"<strong>Data original:</strong> {escape(str(original_date))}<br>"
            f"<strong>Visibilidade original:</strong> {visibility}"
            "</p>"
            "<hr>"
            f"{original_body}"
        )

        response = glpi_request(
            session,
            "POST",
            "ITILFollowup/",
            f"comentário Jira {jira_comment_id}",
            json={
                "input": {
                    "itemtype": "Ticket",
                    "items_id": ticket_id,
                    "content": content,
                    "is_private": 0 if is_public else 1,
                }
            },
            timeout=REQUEST_TIMEOUT,
        )

        glpi_followup_id = int(response.json()["id"])
        imported[jira_comment_id] = glpi_followup_id
        save_state(state)

        print(
            f"Comentário Jira {jira_comment_id} -> "
            f"GLPI Followup {glpi_followup_id} ({visibility})."
        )


# =============================================================================
# FLUXO PRINCIPAL
# =============================================================================


def main() -> int:
    """Executa a migração piloto."""
    section("1. LENDO CHAMADO DO JIRA")

    jira_session = build_jira_session()

    try:
        issue = get_jira_issue(jira_session)
        fields = issue.get("fields") or {}

        summary = fields.get("summary") or ""
        status = (fields.get("status") or {}).get("name") or ""
        attachments = fields.get("attachment") or []
        jira_priority_name = (fields.get("priority") or {}).get("name") or ""
        jira_created = fields.get("created") or ""
        jira_created_formatted = format_jira_datetime(
            jira_created
        )
        description_html = get_jira_description_html(issue)

        request_info = get_jira_request_info(jira_session)
        request_type_name = request_info["request_type_name"]
        reporter_name = request_info["reporter_name"]
        reporter_email = request_info["reporter_email"]
        sector_raw = fields.get(
            JIRA_SECTOR_FIELD_ID
        )
        branch_raw = fields.get(
            JIRA_BRANCH_FIELD_ID
        )
        sector = request_field_value_to_text(
            sector_raw
        )
        branch = request_field_value_to_text(
            branch_raw
        )
        # Fallback para chamados em que eventualmente esses dados
        # apareçam no formulário do JSM em vez dos campos internos.
        if not sector:
            sector = request_info.get("sector", "")

        if not branch:
            branch = request_info.get("branch", "")

        comments = get_jira_comments(jira_session)
        save_json(JIRA_COMMENTS_FILE, comments)

        validate_jira_status(status)

        description_for_glpi = build_glpi_description(
            description_html,
            reporter_name,
            reporter_email,
            jira_created_formatted,
            sector,
            branch,
        )

        print(f"Jira........: {JIRA_KEY}")
        print(f"Abertura Jira: {jira_created_formatted}")
        print(f"Status......: {status}")
        print(f"Título......: {summary}")
        print(f"Anexos......: {len(attachments)}")
        print(f"Descrição...: {len(description_html)} caracteres")
        print(f"Prioridade..: {jira_priority_name}")
        print(f"Tipo Jira...: {request_type_name}")
        print(f"Requerente..: {reporter_name}")
        print(f"E-mail......: {reporter_email}")
        print(f"Comentários.: {len(comments)}")
        print(f"RAW Setor...: {sector_raw!r}")#temporario--------------------------------------------------------------------
        print(f"RAW Filial..: {branch_raw!r}")#temporario--------------------------------------------------------------------
        print(
            f"Setor.......: "
            f"{sector or 'Não informado'}"
        )

        print(
            f"Filial......: "
            f"{branch or 'Não informado'}"
        )
        print(
            f"Abertura....: "
            f"{jira_created_formatted}"
        )
        section("2. BAIXANDO ANEXOS DO JIRA")
        downloaded = download_jira_attachments(jira_session, attachments)

    finally:
        jira_session.close()

    section("3. RESUMO DO PILOTO")
    print(f"Título: {summary}")
    print(f"Status: {status}")
    print(f"Anexos baixados: {len(downloaded)}")

    for item in downloaded:
        print(f"  - {item['filename']} ({item['size']} bytes)")

    if DRY_RUN:
        section("DRY-RUN CONCLUÍDO")
        print("Nenhum dado foi gravado no GLPI.")
        return 0

    state = load_state()

    section("4. AUTENTICANDO NO GLPI")
    glpi_session = init_glpi_session()

    try:
        section("5. RESOLVENDO METADADOS E SINCRONIZANDO TICKET")

        category_id = resolve_glpi_category_id(
            glpi_session,
            request_type_name,
        )
        priority = resolve_glpi_priority(jira_priority_name)
        glpi_status = resolve_glpi_status(status)

        print(f"Categoria...: {request_type_name} -> GLPI {category_id}")
        print(f"Prioridade..: {jira_priority_name} -> GLPI {priority}")
        print(
            f"Status......: {status} -> "
            f"GLPI {glpi_status} "
            f"({GLPI_STATUS_LABELS[glpi_status]})"
        )

        ticket_id = sync_glpi_ticket(
            glpi_session,
            state,
            summary=summary,
            content=description_for_glpi,
            category_id=category_id,
            priority=priority,
            status_id=glpi_status,
        )

        ensure_glpi_requester(glpi_session, ticket_id)

        section("6. TRANSFERINDO ANEXOS PARA O GLPI")
        transfer_attachments_to_glpi(
            glpi_session,
            ticket_id,
            downloaded,
            state,
        )

        section("7. TRANSFERINDO COMENTÁRIOS")
        import_jira_comments(
            glpi_session,
            ticket_id,
            comments,
            state,
        )

        section("8. AJUSTANDO STATUS FINAL")
        enforce_glpi_ticket_status(
            glpi_session,
            ticket_id,
            glpi_status,
        )

        section("PILOTO CONCLUÍDO")
        print(f"Jira........: {JIRA_KEY}")
        print(f"GLPI........: {ticket_id}")
        print(f"Título......: {summary}")
        print(f"Anexos......: {len(state['uploaded_attachments'])}")
        print(f"Comentários.: {len(state['imported_comments'])}")

        return 0

    finally:
        close_glpi_session(glpi_session)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nExecução interrompida pelo usuário.")
        sys.exit(130)
