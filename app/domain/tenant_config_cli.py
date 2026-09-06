"""CLI de validacao de config de tenant.

    python -m app.domain.tenant_config_cli --validate config/tenants/clinica-exemplo.yaml
    python -m app.domain.tenant_config_cli --validate-all

Sai com codigo 1 se algum arquivo for invalido — o CI depende disso.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.core.config import get_settings
from app.domain.tenant_config import TenantConfigError, load_tenant_config


def _validate(paths: list[Path]) -> int:
    failures = 0
    for path in paths:
        try:
            config = load_tenant_config(path)
        except TenantConfigError as exc:
            failures += 1
            print(f"FALHOU  {path.name}\n        {exc}", file=sys.stderr)
        else:
            channels = [
                name
                for name, on in (
                    (
                        "whatsapp",
                        config.channels.whatsapp is not None and config.channels.whatsapp.enabled,
                    ),
                    ("web", config.channels.web.enabled),
                )
                if on
            ]
            print(
                f"OK      {path.name}  "
                f"[{config.identity.vertical}] agente={config.persona.agent_name} "
                f"canais={'+'.join(channels)} "
                f"intake={len(config.intake.field_keys())} campos"
            )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tenant-config")
    parser.add_argument("--validate", nargs="*", type=Path, metavar="YAML")
    parser.add_argument("--validate-all", action="store_true")
    args = parser.parse_args(argv)

    if args.validate_all:
        directory = Path(get_settings().tenants_config_dir)
        paths = [p for p in sorted(directory.glob("*.yaml")) if not p.stem.startswith("_")]
    elif args.validate is not None:
        paths = list(args.validate)
    else:
        parser.error("informe --validate <arquivo> ou --validate-all")

    if not paths:
        print("nenhum arquivo de tenant encontrado", file=sys.stderr)
        return 1

    failures = _validate(paths)
    if failures:
        print(f"\n{failures} de {len(paths)} arquivos invalidos", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
