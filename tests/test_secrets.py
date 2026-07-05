import pytest

from resoluto.sandbox.secrets import SecretKeyRef, SecretProvider, secrets_from_env


def test_secrets_from_env_returns_none_when_kind_unset():
    assert secrets_from_env({}) is None


def test_secrets_from_env_raises_for_any_kind():
    with pytest.raises(RuntimeError, match="no SecretProvider implementation ships"):
        secrets_from_env({"RESOLUTO_SECRETS_KIND": "vault"})


def test_secret_key_ref_is_a_frozen_dataclass():
    ref = SecretKeyRef("anthropic-key", "api_key")
    assert ref.name == "anthropic-key"
    assert ref.key == "api_key"
    with pytest.raises(AttributeError):
        ref.name = "other"


def test_secret_provider_is_abstract():
    with pytest.raises(TypeError):
        SecretProvider()


@pytest.mark.asyncio
async def test_secret_provider_subclass_must_implement_get():
    class _Fixed(SecretProvider):
        async def get(self, ref: str) -> str:
            return f"resolved:{ref}"

    provider = _Fixed()
    assert await provider.get("secret/data/x#key") == "resolved:secret/data/x#key"
