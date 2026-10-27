"""Synthetic provider catalog shared by offline settings tests."""


def catalog_values():
    return {
        "providers": [
            {"id": "primary", "name": "Primary", "kind": "openai",
             "base_url": "https://primary.invalid/v1", "api_key": "sk-primary-fixture-1234",
             "models": [{"id": "fast", "name": "Fast", "model": "fast-model"},
                        {"id": "reason", "name": "Reason", "model": "reason-model",
                         "thinking": "enabled", "timeout_seconds": 180}]},
            {"id": "deepseek", "name": "DeepSeek", "kind": "deepseek",
             "base_url": "https://deepseek.invalid/v1", "api_key": "sk-deepseek-fixture-5678",
             "models": [{"id": "chat", "name": "Chat", "model": "deepseek-chat",
                         "profile": "deepseek", "thinking": "disabled"}]},
        ],
        "bindings": {"agent": {"provider_id": "primary", "model_id": "reason"},
                     "research": {"provider_id": "deepseek", "model_id": "chat"}},
    }
