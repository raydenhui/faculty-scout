"""Interactive chat agent for fscout configuration and queries."""

from __future__ import annotations

import json

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.panel import Panel

from .config import AppConfig
from .schema import ColumnDef, Schema

console = Console()

_STYLE = Style.from_dict({"prompt": "bold ansicyan"})


def _help_text() -> str:
    return """Available commands:
  /list          List configured universities from Excel
  /schema        Show current schema columns
  /add-col NAME TYPE [hint HINT] [formula FORMULA] [value VALUE] [value_from FIELD]
                 Add a column to schema.json
  /export        Export Excel from output file
  /config        Show current config (secrets masked)
  /help          Show this help
  /exit, /quit   Exit chat"""


async def run_chat(config: AppConfig, schema: Schema) -> None:
    """Start the interactive chat REPL."""
    console.print(
        Panel.fit(
            "[bold blue]fscout Chat[/]\nType [cyan]/help[/] for available commands.",
            border_style="blue",
        )
    )

    session = PromptSession(
        history=FileHistory(".fscout_chat_history"),
        style=_STYLE,
    )

    while True:
        try:
            line = await session.prompt_async("fscout> ", style=_STYLE)
        except (EOFError, KeyboardInterrupt):
            console.print("\nGoodbye!")
            break

        line = line.strip()
        if not line:
            continue

        if line.startswith("/"):
            await _handle_command(line, config, schema)
        else:
            await _handle_natural_language(line, config, schema)


async def _handle_command(line: str, config: AppConfig, schema: Schema) -> None:
    parts = line.split()
    cmd = parts[0].lower()

    if cmd in ("/exit", "/quit"):
        raise EOFError

    if cmd == "/help":
        console.print(_help_text())

    elif cmd == "/list":
        from .pipeline import read_targets

        targets = read_targets(config.files.input_excel)
        if not targets:
            console.print("[yellow]No universities configured.[/]")
        else:
            for t in targets:
                dept = t["department"] or "[all departments]"
                status = f" [{t['status']}]" if t.get("status") else ""
                console.print(f"  • {t['university']} — {dept}{status}")

    elif cmd == "/schema":
        if not schema.columns:
            console.print("[yellow]No schema columns defined.[/]")
        else:
            for col in schema.columns:
                extra = ""
                if col.hint:
                    extra += f" hint={col.hint}"
                if col.formula:
                    extra += f" formula={col.formula}"
                if col.value:
                    extra += f" value={col.value}"
                if col.value_from:
                    extra += f" value_from={col.value_from}"
                console.print(f"  [{col.type}][/] {col.name}{extra}")

    elif cmd == "/add-col":
        await _add_column(parts[1:], schema, config)

    elif cmd == "/export":
        console.print(f"[yellow]Output file: {config.files.output_excel}[/]")
        console.print("[dim]Run 'fscout export' or 'fscout run' to generate fresh output.[/]")

    elif cmd == "/config":
        from .config import mask_secrets
        console.print_json(json.dumps(mask_secrets(config)))

    else:
        console.print(f"[red]Unknown command: {cmd}[/]. Type /help for available commands.")


async def _add_column(args: list[str], schema: Schema, config: AppConfig) -> None:
    if len(args) < 2:
        console.print(
            "[red]Usage: /add-col NAME TYPE [hint HINT] [formula FORMULA] [value VALUE] [value_from FIELD][/]"
        )
        return

    name = args[0]
    col_type = args[1]
    if col_type not in ("extracted", "fallback", "formula", "static"):
        console.print(f"[red]Invalid type: {col_type}. Must be extracted, fallback, formula, or static.[/]")
        return

    hint = None
    formula = None
    value = None
    value_from = None

    i = 2
    while i < len(args):
        if args[i] == "hint" and i + 1 < len(args):
            hint = args[i + 1]
            i += 2
        elif args[i] == "formula" and i + 1 < len(args):
            formula = args[i + 1]
            i += 2
        elif args[i] == "value" and i + 1 < len(args):
            value = args[i + 1]
            i += 2
        elif args[i] == "value_from" and i + 1 < len(args):
            value_from = args[i + 1]
            i += 2
        else:
            i += 1

    col = ColumnDef(name=name, type=col_type, hint=hint, formula=formula, value=value, value_from=value_from)
    schema.columns.append(col)

    from pathlib import Path
    path = Path(config.files.schema_file)
    path.write_text(schema.model_dump_json(indent=2), encoding="utf-8")
    console.print(f"[green]Added column '{name}' ({col_type}) to {path}[/]")


async def _handle_natural_language(text: str, config: AppConfig, schema: Schema) -> None:
    text_lower = text.lower().strip()

    if "list universit" in text_lower:
        from .pipeline import read_targets

        targets = read_targets(config.files.input_excel)
        for t in targets:
            dept = t["department"] or "[all]"
            console.print(f"  • {t['university']} — {dept}")

    elif any(w in text_lower for w in ("export", "results", "data")):
        console.print(f"[yellow]Output file is {config.files.output_excel}. Run 'fscout run' to scrape fresh data.[/]")

    elif "schema" in text_lower:
        console.print(f"[dim]Schema file: {config.files.schema_file}. Use /schema to view, /add-col to add.[/]")

    else:
        console.print("[dim]Type /help for available commands.[/]")
