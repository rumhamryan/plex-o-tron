from __future__ import annotations

import importlib
from pathlib import Path


def _iter_service_packages() -> list[str]:
    root = Path(__file__).resolve().parents[2]
    services_dir = root / "telegram_bot" / "services"
    packages: list[str] = []
    for entry in services_dir.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith("__"):
            continue
        if not (entry / "__init__.py").exists():
            continue
        packages.append(entry.name)
    return sorted(packages)


def _iter_package_modules(package_dir: Path) -> set[str]:
    module_names: set[str] = set()
    for entry in package_dir.iterdir():
        if entry.name.startswith("__"):
            continue
        if entry.is_dir():
            if (entry / "__init__.py").exists():
                module_names.add(entry.name)
            continue
        if entry.suffix == ".py":
            module_names.add(entry.stem)
    return module_names


def test_service_packages_define_all_and_no_unexpected_exports() -> None:
    packages = _iter_service_packages()
    assert packages, "No service packages found to validate."

    root = Path(__file__).resolve().parents[2]
    services_dir = root / "telegram_bot" / "services"

    for name in packages:
        package_dir = services_dir / name
        module = importlib.import_module(f"telegram_bot.services.{name}")
        assert hasattr(module, "__all__"), f"{name} is missing __all__."
        exported = getattr(module, "__all__")
        assert isinstance(exported, (list, tuple)), f"{name}.__all__ must be a list or tuple."
        assert all(
            isinstance(item, str) for item in exported
        ), f"{name}.__all__ must contain only strings."

        missing = [item for item in exported if not hasattr(module, item)]
        assert not missing, f"{name}.__all__ has missing names: {missing}"

        public_names = {item for item in dir(module) if not item.startswith("_")}
        allowed_submodules = _iter_package_modules(package_dir)
        unexpected = sorted(public_names - set(exported) - allowed_submodules)
        assert not unexpected, f"{name} has unexpected public exports: {unexpected}"
