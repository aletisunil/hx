"""The browser half of an authorization-code sign-in, shared by every route.

A route supplies the authorize URL and the loopback port it registered; this
opens the browser, listens for the redirect, and races it against a paste
prompt. What the route does with the code - which token endpoint, which body -
stays in the route's own module.
"""

from __future__ import annotations

import asyncio
import webbrowser
from typing import Protocol

from hx.auth.oauth.callback import CallbackError, CallbackResult, LoopbackCallback, parse_redirect


class OAuthError(Exception):
    """A sign-in that could not complete. The message is shown as-is."""


class LoginInteraction(Protocol):
    """How a login flow talks to whoever started it (CLI prompt or TUI modal)."""

    def show_url(self, url: str, instructions: str) -> None: ...

    def show_device_code(self, user_code: str, verification_uri: str) -> None: ...

    def progress(self, message: str) -> None: ...

    async def prompt_paste(self, message: str) -> str:
        """Return a pasted redirect URL or code.

        May never return - the browser callback usually wins the race - so
        implementations must tolerate cancellation.
        """
        ...


async def authorize_in_browser(
    url: str,
    *,
    port: int,
    path: str,
    state: str,
    interaction: LoginInteraction,
    instructions: str = "",
) -> CallbackResult:
    """Send the user to ``url`` and return the code the provider hands back.

    The callback server and the paste prompt race each other: over SSH the
    browser opens on the wrong machine and can never reach the loopback port,
    so pasting the final redirect URL has to work just as well.

    ``instructions`` is anything route-specific the user needs to know about
    the page they are about to see, said before the generic part.

    Raises:
        OAuthError: when the returned state is not the one sent.
        CallbackError: when the provider reports a failure or nothing usable
            was pasted.
    """
    callback = LoopbackCallback(port, path, state=state)
    callback.start()
    try:
        interaction.show_url(
            url,
            (f"{instructions} " if instructions else "")
            + "Complete the sign-in in your browser. On a remote machine, paste the "
            "final redirect URL here instead.",
        )
        if not callback.listening:
            interaction.progress(
                f"Port {port} is busy, so the browser cannot hand the code back. "
                "Paste the redirect URL here instead."
            )
        elif not await open_browser(url):
            interaction.progress("Could not open a browser - open the URL above manually.")

        result = await _race_callback_and_paste(callback, interaction)
    finally:
        try:
            await callback.aclose()
        except asyncio.CancelledError:
            # Cancelled mid-teardown: finish the job on this thread rather than
            # leaving the port bound for the life of the process, which would
            # cost the *next* login its browser callback.
            callback.close()
            raise

    if result.state is not None and result.state != state:
        raise OAuthError("OAuth state mismatch - discard this login and try again.")
    return result


async def _race_callback_and_paste(
    callback: LoopbackCallback,
    interaction: LoginInteraction,
) -> CallbackResult:
    paste_task = asyncio.ensure_future(
        interaction.prompt_paste("Paste the redirect URL or authorization code:")
    )
    wait_task = asyncio.ensure_future(callback.wait())
    try:
        done, _ = await asyncio.wait({paste_task, wait_task}, return_when=asyncio.FIRST_COMPLETED)
        # Prefer the loopback result: it is the one whose state we validated.
        if wait_task in done and not wait_task.cancelled():
            exc = wait_task.exception()
            if exc is None:
                return wait_task.result()
            if paste_task not in done:
                raise exc
        if paste_task in done:
            return parse_redirect(paste_task.result())
        raise CallbackError("Login did not complete.")
    finally:
        for task in (paste_task, wait_task):
            if not task.done():
                task.cancel()


async def open_browser(url: str) -> bool:
    """Open the URL without stalling the caller's event loop.

    ``webbrowser.open`` is not a quick handoff: on macOS it writes AppleScript
    to ``osascript`` and waits for it, which takes as long as the browser takes
    to come up. On the event loop that freezes the whole TUI - including the
    Escape that cancels the login and the field the user is meant to paste into.
    """
    try:
        return await asyncio.to_thread(webbrowser.open, url)
    except webbrowser.Error:
        return False
