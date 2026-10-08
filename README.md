# Migração Jira Service Management -> GLPI

Projeto reconstruído a partir do migrador já validado anteriormente.

## Arquivos principais

- `pilot_migration.py`: migra um único chamado Jira.
- `batch_migration.py`: executa o migrador em lote via JQL.
- `recover_state.py`: reconstrói a idempotência mínima quando `pilot_data/` foi perdido.

## O que fazer porque o pilot_data foi perdido

**Não rode o batch real antes do recovery.**

Os tickets que já foram migrados continuam no GLPI. O problema da perda do
`pilot_data` é que perdemos a relação local de ticket/anexo/comentário.

O `recover_state.py` procura tickets existentes usando:

1. `externalid = TAREFA-XXX`; ou
2. o texto `Importado do Jira: TAREFA-XXX` no conteúdo do ticket.

Para cada chave encontrada uma única vez ele recria:

`pilot_data/TAREFA-XXX/state.json`

com `recovered_existing=true`.

O `pilot_migration.py` detecta essa marcação e faz **SKIP integral** do ticket
para não duplicar anexos e comentários.

Se houver dois ou mais tickets GLPI para a mesma Jira Key, o recovery não
escolhe automaticamente. Ele grava `recovery_duplicates.json` para revisão.

## Instalação

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r .\requirements.txt

Copy-Item .\scripts\set-env.ps1.example .\scripts\set-env.ps1
# Edite tokens/credenciais.
. .\scripts\set-env.ps1
```

## 1. Recovery obrigatório

```powershell
$env:RECOVERY_OVERWRITE="0"
py .\recover_state.py
```

Revise:

- `recovery_registry.json`
- `recovery_duplicates.json`
- `pilot_data\*\state.json`

Se `recovery_duplicates.json` não estiver vazio, resolva essas chaves antes de
rodar o lote real.

## 2. Teste individual

```powershell
$env:JIRA_ISSUE_KEY="TAREFA-603"
$env:DRY_RUN="1"
py .\pilot_migration.py
```

## 3. Descoberta do lote

O JQL padrão é:

```text
project = TAREFA
AND statusCategory = Done
AND status NOT IN ("Cancelar Solicitação", "Cancelado")
ORDER BY key ASC
```

Isso inclui resolvidos com resolução `Itens concluídos` e `Finalizado`.

```powershell
$env:MIGRATION_SCOPE="done"
$env:BATCH_DISCOVERY_ONLY="1"
$env:BATCH_LIMIT="0"
$env:DRY_RUN="1"
py .\batch_migration.py
```

## 4. Microbatch

```powershell
$env:BATCH_DISCOVERY_ONLY="0"
$env:BATCH_LIMIT="3"
$env:DRY_RUN="1"
py .\batch_migration.py
```

## 5. Microbatch real

```powershell
$env:BATCH_LIMIT="3"
$env:DRY_RUN="0"
$env:BATCH_CONFIRM="MIGRAR"
py .\batch_migration.py
```

## 6. Lote completo

```powershell
$env:BATCH_LIMIT="0"
$env:BATCH_DISCOVERY_ONLY="0"
$env:DRY_RUN="0"
$env:BATCH_CONFIRM="MIGRAR"
py .\batch_migration.py
```

## Campos migrados

- título;
- descrição;
- data de abertura original;
- requerente original;
- setor (`customfield_10154`);
- filial (`customfield_10119`);
- prioridade;
- tipo de solicitação -> categoria GLPI de mesmo nome;
- status;
- requerente GLPI fixo `jira` ID 96;
- anexos;
- comentários públicos e internos;
- limpeza de respostas de e-mail;
- `externalid = TAREFA-XXX` nos novos tickets.

## Commits sugeridos

```text
chore: bootstrap jira-glpi migration project
chore: add migration environment configuration
feat(jira): restore issue metadata extraction
feat(glpi): restore ticket migration workflow
feat(migration): restore attachments and comment import
feat(batch): restore sequential jira to glpi batch migration
feat(recovery): rebuild idempotency from existing glpi tickets
fix(batch): include all done-category jira resolutions
docs: add migration recovery and execution runbook
```
