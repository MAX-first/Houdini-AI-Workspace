"""Codex 执行模型目录与独立偏好配置。"""

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CodexModel:
    """保存 App Server 实际返回的模型能力。"""

    id: str
    model: str
    display_name: str
    is_default: bool
    default_reasoning_effort: str | None
    supported_reasoning_efforts: tuple[tuple[str, str], ...]
    input_modalities: tuple[str, ...]

    @property
    def reasoning_efforts(self):
        """返回当前模型支持的推理强度 ID。"""
        return tuple(effort for effort, _description in self.supported_reasoning_efforts)


def parse_model_catalog(entries):
    """将可见模型响应转换为可供选择的模型目录。"""
    models = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("hidden"):
            continue
        model_id = entry.get("id")
        model_name = entry.get("model")
        if not isinstance(model_id, str) or not model_id or not isinstance(model_name, str) or not model_name:
            continue
        efforts = entry.get("supportedReasoningEfforts") or []
        supported = tuple((item["reasoningEffort"], item.get("description", "")) for item in efforts
                          if isinstance(item, dict) and isinstance(item.get("reasoningEffort"), str))
        modalities = entry.get("inputModalities")
        if modalities is None:
            modalities = ["text", "image"]
        models.append(CodexModel(
            id=model_id, model=model_name, display_name=entry.get("displayName") or model_id,
            is_default=entry.get("isDefault") is True,
            default_reasoning_effort=entry.get("defaultReasoningEffort"),
            supported_reasoning_efforts=supported,
            input_modalities=tuple(modalities),
        ))
    return tuple(models)


def resolve_execution_model(catalog, selected_model_id=None, selected_effort=None):
    """只从当前目录中选择模型和其支持的推理强度。"""
    if not catalog:
        raise ValueError("当前账户没有可用的 Codex 执行模型")
    by_id = {model.id: model for model in catalog}
    model = by_id.get(selected_model_id) if selected_model_id else None
    model = model or next((item for item in catalog if item.is_default), catalog[0])
    efforts = model.reasoning_efforts
    effort = selected_effort if selected_effort in efforts else model.default_reasoning_effort
    if effort not in efforts:
        effort = efforts[0] if efforts else None
    return model, effort


class ExecutionModelSettings:
    """单独保存 Codex 执行模型偏好，不修改现有聊天模型设置。"""

    def __init__(self, path=None):
        if path is None:
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
            path = base / "HoudiniAgent" / "codex_execution.json"
        self.path = Path(path)

    def read(self):
        """读取已保存的模型 ID 和推理强度。"""
        if not self.path.exists():
            return {"model_id": None, "reasoning_effort": None}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Codex 执行模型配置格式无效")
        return {"model_id": data.get("model_id"), "reasoning_effort": data.get("reasoning_effort")}

    def save(self, catalog, model_id, reasoning_effort=None):
        """验证当前可用模型后保存执行偏好。"""
        model = next((item for item in catalog if item.id == model_id), None)
        if model is None:
            raise ValueError("所选 Codex 执行模型不在当前可用列表中")
        if reasoning_effort is not None and reasoning_effort not in model.reasoning_efforts:
            raise ValueError("所选推理强度不受当前模型支持")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps({"model_id": model_id, "reasoning_effort": reasoning_effort},
                                        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)
