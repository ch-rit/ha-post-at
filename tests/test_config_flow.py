"""Tests for the config flow."""

from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from post_at import ApiError, AuthenticationError, InteractionRequiredError, PendingLogin
import pytest

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.post_at.const import (
    CONF_KEEP_DELIVERED_DAYS,
    CONF_REDIRECT_URL,
    CONF_SESSION,
    DOMAIN,
)

from .conftest import ACCOUNT_ID, EMAIL, make_session

PENDING = PendingLogin("https://login.post.at/authorize?state=abc", "abc", "verifier")
REDIRECT_URL = "msal228c4360-3391-4b1c-93a2-e5d7d946b647://at.post.app/auth?code=c&state=abc"
CREDENTIALS = {CONF_EMAIL: EMAIL, CONF_PASSWORD: "secret"}


@pytest.fixture
def flow_client() -> Generator[MagicMock]:
    """Mock the client the config flow signs in with."""
    with patch(
        "custom_components.post_at.config_flow.PostAtClient", autospec=True
    ) as client_class:
        client = client_class.return_value
        client.__enter__.return_value = client
        client.login.return_value = make_session()
        client.begin_browser_login.return_value = PENDING
        client.complete_browser_login.return_value = make_session("browser-refresh")
        yield client


async def test_user_flow(
    hass: HomeAssistant, flow_client: MagicMock, mock_setup_entry: AsyncMock
) -> None:
    """Signing in creates an entry that stores only the session."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: f" {EMAIL} ", CONF_PASSWORD: "secret"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == EMAIL
    assert result["data"] == {CONF_SESSION: make_session().to_dict()}
    assert result["result"].unique_id == ACCOUNT_ID
    flow_client.login.assert_called_once_with(EMAIL, "secret")
    assert len(mock_setup_entry.mock_calls) == 1


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (AuthenticationError("AADB2C90225: wrong password"), "invalid_auth"),
        (ApiError(503, "unavailable", None), "cannot_connect"),
        (httpx.ConnectError("unreachable"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant,
    flow_client: MagicMock,
    mock_setup_entry: AsyncMock,
    error: Exception,
    reason: str,
) -> None:
    """Sign-in errors are shown and the flow can recover."""
    flow_client.login.side_effect = error
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}

    flow_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("claims", "unique_id"),
    [
        ({"username": "Someone@Example.com"}, "someone@example.com"),
        ({}, EMAIL),
    ],
)
async def test_user_flow_unique_id_fallback(
    hass: HomeAssistant,
    flow_client: MagicMock,
    mock_setup_entry: AsyncMock,
    claims: dict[str, str],
    unique_id: str,
) -> None:
    """Without an SSO id the lower-cased user name or e-mail is the unique id."""
    flow_client.login.return_value = make_session(claims=claims)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL.upper(), CONF_PASSWORD: "secret"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == unique_id


async def test_browser_fallback(
    hass: HomeAssistant, flow_client: MagicMock, mock_setup_entry: AsyncMock
) -> None:
    """When the login needs interaction, the user signs in with a browser."""
    flow_client.login.side_effect = InteractionRequiredError("MFA")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "browser"
    assert result["description_placeholders"] == {"url": PENDING.url}

    flow_client.complete_browser_login.side_effect = AuthenticationError("state mismatch")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REDIRECT_URL: "msal://wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "browser"
    assert result["errors"] == {"base": "invalid_redirect_url"}

    flow_client.complete_browser_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REDIRECT_URL: f"  {REDIRECT_URL}\n"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == EMAIL
    assert result["data"] == {CONF_SESSION: make_session("browser-refresh").to_dict()}
    flow_client.complete_browser_login.assert_called_with(PENDING, REDIRECT_URL)
    # The same pending login is reused for every attempt.
    flow_client.begin_browser_login.assert_called_once()


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx.ReadTimeout("timeout"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_browser_errors(
    hass: HomeAssistant,
    flow_client: MagicMock,
    mock_setup_entry: AsyncMock,
    error: Exception,
    reason: str,
) -> None:
    """Errors while redeeming the redirect URL are shown."""
    flow_client.login.side_effect = InteractionRequiredError("MFA")
    flow_client.complete_browser_login.side_effect = error
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REDIRECT_URL: REDIRECT_URL}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}


async def test_already_configured(
    hass: HomeAssistant,
    flow_client: MagicMock,
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """An account can be added only once."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant,
    flow_client: MagicMock,
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Re-authentication replaces the stored session."""
    mock_config_entry.add_to_hass(hass)
    flow_client.login.side_effect = AuthenticationError("wrong password")
    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"] == {"name": EMAIL}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["errors"] == {"base": "invalid_auth"}

    flow_client.login.side_effect = None
    flow_client.login.return_value = make_session("new-refresh")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_SESSION]["refresh_token"] == "new-refresh"
    assert CONF_PASSWORD not in mock_config_entry.data


async def test_reauth_browser(
    hass: HomeAssistant,
    flow_client: MagicMock,
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Re-authentication falls back to the browser sign-in as well."""
    mock_config_entry.add_to_hass(hass)
    flow_client.login.side_effect = InteractionRequiredError("MFA")
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["step_id"] == "browser"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REDIRECT_URL: REDIRECT_URL}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_SESSION]["refresh_token"] == "browser-refresh"


async def test_reauth_wrong_account(
    hass: HomeAssistant,
    flow_client: MagicMock,
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Re-authentication with another account is refused."""
    mock_config_entry.add_to_hass(hass)
    flow_client.login.return_value = make_session(
        "other-refresh", {"extension_SSOID": "someone-else"}
    )
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert mock_config_entry.data[CONF_SESSION]["refresh_token"] == "refresh-1"


async def test_options_flow(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client_class: MagicMock
) -> None:
    """Changing the options reloads the entry with the new interval."""
    assert init_integration.runtime_data.update_interval == timedelta(minutes=15)
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 30.0, CONF_KEEP_DELIVERED_DAYS: 7.0}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.options == {CONF_SCAN_INTERVAL: 30, CONF_KEEP_DELIVERED_DAYS: 7}
    assert init_integration.runtime_data.update_interval == timedelta(minutes=30)
    assert mock_client_class.call_count == 2


async def test_options_flow_validation(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The interval has a minimum and the retention a range."""
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    with pytest.raises(InvalidData, match="scan_interval"):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SCAN_INTERVAL: 4, CONF_KEEP_DELIVERED_DAYS: 3}
        )
    with pytest.raises(InvalidData, match="keep_delivered_days"):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SCAN_INTERVAL: 15, CONF_KEEP_DELIVERED_DAYS: 31}
        )
