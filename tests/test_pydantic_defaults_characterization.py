from __future__ import annotations

import inspect

from pydantic import BaseModel


def _model_fields(model: type[BaseModel]) -> dict:
    if hasattr(model, "model_fields"):
        return model.model_fields
    return model.__fields__


def test_pydantic_models_do_not_use_mutable_field_defaults():
    from app.routers import auth, chat, memory, sessions

    offenders: list[str] = []
    for module in (auth, chat, memory, sessions):
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if not issubclass(obj, BaseModel) or obj is BaseModel:
                continue
            for field_name, field in _model_fields(obj).items():
                default = getattr(field, "default", None)
                if isinstance(default, (dict, list, set)):
                    offenders.append(f"{obj.__module__}.{obj.__name__}.{field_name}")

    assert offenders == []
