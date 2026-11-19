"""Config flow for the Österreichische Post integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
from post_at import (
    ApiError,
    AuthenticationError,
    InteractionRequiredError,
    PendingLogin,
    PostAtClient,
    Session,
)
import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_KEEP_DELIVERED_DAYS,
    CONF_REDIRECT_URL,
    CONF_SESSION,
    DEFAULT_KEEP_DELIVERED_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
    MAX_KEEP_DELIVERED_DAYS,
    MIN_SCAN_INTERVAL,
)
from .coordinator import PostAtConfigEntry

LOGIN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)
BROWSER_SCHEMA = vol.Schema({vol.Required(CONF_REDIRECT_URL): TextSelector()})
OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): NumberSelector(
            NumberSelectorConfig(
                min=MIN_SCAN_INTERVAL,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="min",
            )
        ),
        vol.Required(
            CONF_KEEP_DELIVERED_DAYS, default=DEFAULT_KEEP_DELIVERED_DAYS
        ): NumberSelector(
            NumberSelectorConfig(
                min=0,
                max=MAX_KEEP_DELIVERED_DAYS,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="d",
            )
        ),
    }
)


def _login(email: str, password: str) -> Session:
    with PostAtClient() as client:
        return client.login(email, password)


def _begin_browser_login() -> PendingLogin:
    with PostAtClient() as client:
        return client.begin_browser_login()


def _complete_browser_login(pending: PendingLogin, redirect_url: str) -> Session:
    with PostAtClient() as client:
        return client.complete_browser_login(pending, redirect_url)


def _account_id(session: Session, email: str) -> str:
    """Return the stable account id: the SSO id, else the user name or e-mail."""
    claims = session.claims
    if sso_id := claims.get("extension_SSOID"):
        return str(sso_id)
    return str(claims.get("username") or email).lower()


class PostAtConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sign in to a Post account."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._email = ""
        self._pending: PendingLogin | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: PostAtConfigEntry) -> PostAtOptionsFlow:
        """Create the options flow."""
        return PostAtOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in with e-mail and password."""
        return await self._async_step_login("user", user_input)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication after the session expired."""
        self._email = self._get_reauth_entry().title
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in again; the description shows the entry title as ``{name}``."""
        return await self._async_step_login("reauth_confirm", user_input)

    async def _async_step_login(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            try:
                session = await self.hass.async_add_executor_job(
                    _login, self._email, user_input[CONF_PASSWORD]
                )
            except InteractionRequiredError:
                return await self.async_step_browser()
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except (ApiError, httpx.HTTPError):
                errors["base"] = "cannot_connect"
            except Exception:
                LOGGER.exception("Unexpected error during sign-in")
                errors["base"] = "unknown"
            else:
                return await self._async_finish(session)
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                LOGIN_SCHEMA, {CONF_EMAIL: self._email}
            ),
            errors=errors,
        )

    async def async_step_browser(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in with a browser when the login needs MFA or other interaction."""
        errors: dict[str, str] = {}
        if self._pending is None:
            self._pending = await self.hass.async_add_executor_job(_begin_browser_login)
        if user_input is not None:
            try:
                session = await self.hass.async_add_executor_job(
                    _complete_browser_login,
                    self._pending,
                    user_input[CONF_REDIRECT_URL].strip(),
                )
            except AuthenticationError:
                errors["base"] = "invalid_redirect_url"
            except (ApiError, httpx.HTTPError):
                errors["base"] = "cannot_connect"
            except Exception:
                LOGGER.exception("Unexpected error during browser sign-in")
                errors["base"] = "unknown"
            else:
                return await self._async_finish(session)
        return self.async_show_form(
            step_id="browser",
            data_schema=BROWSER_SCHEMA,
            errors=errors,
            description_placeholders={"url": self._pending.url},
        )

    async def _async_finish(self, session: Session) -> ConfigFlowResult:
        """Create or update the entry; only the session is stored."""
        await self.async_set_unique_id(_account_id(session, self._email))
        data = {CONF_SESSION: session.to_dict()}
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates=data
            )
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=self._email, data=data)


class PostAtOptionsFlow(OptionsFlowWithReload):
    """Polling and retention options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={key: int(value) for key, value in user_input.items()}
            )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options
            ),
        )
