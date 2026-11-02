"""Tests for diagnostic data sanitisation."""

from custom_components.hikvision_next.diagnostics import anonymise_data, get_isapi_data


async def test_diagnostics_do_not_include_exception_messages() -> None:
    """Diagnostic errors must not leak request URLs or credentials."""

    class BrokenClient:
        async def request(self, method: str, endpoint: str) -> None:
            raise RuntimeError("https://user:secret@camera.local/request failed")

    result = await get_isapi_data(BrokenClient(), "System/deviceInfo")

    assert result == {"error": "RuntimeError"}
    assert "secret" not in str(result)
    assert "camera.local" not in str(result)


def test_anonymise_data_handles_non_string_sensitive_values() -> None:
    """Unexpected structured values for sensitive keys should not crash diagnostics."""

    value = {"serialNumber": {"unexpected": "value"}}

    assert anonymise_data(value) == value
