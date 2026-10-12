"""
AlterChat - main.py
===================

Desktop application entry point and main controller.

Responsibilities:
* Initializes the data directory structure and loads configuration.
* Handles the first-boot Terms & Conditions (EULA) modal, blocking main usage
  until agreed.
* Sets up CustomTkinter window with a 30% / 70% grid layout (WhatsApp Web clone).
* Manages the lifecycle of the single background AI engine (OpenAI-compatible API).
* Connects the Sidebar, Chat Window, Add Contact Modal, and Settings Modal.
* Coordinates continuous history saving and file-based keyword RAG retrieval.
"""

from __future__ import annotations

import logging
import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import customtkinter as ctk

import ai_engine as ai
import data_manager as dm
import ui_components as ui

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("alterchat.main")


class AlterChatApp(ctk.CTk):
    """Main application controller and top-level window."""

    def __init__(self) -> None:
        super().__init__()

        # 1. Initialize data layer & directories
        self.data_manager = dm.DataManager()
        self.data_manager.ensure_data_structure()
        self.settings = self.data_manager.load_settings()

        # 2. Configure appearance & language
        ui.set_language(self.settings.get("language", "en"))
        theme = self.settings.get("theme", "System")
        ctk.set_appearance_mode(theme)
        ctk.set_default_color_theme("blue")

        # 3. Main window geometry & metadata
        self.title(ui.tr("app_title"))
        self.geometry("1100x720")
        self.minsize(860, 560)
        ui.center_window(self, 1100, 720)

        # 4. Dispatcher for thread-safe UI updates
        self.dispatcher = ui.UIDispatcher(self)

        # 5. Shared fonts & state
        self.fonts = ui.build_fonts()
        self.active_contact: Optional[dict[str, Any]] = None
        self._streaming_text = ""
        self._current_gen_contact_id: Optional[str] = None
        self._add_dialog: Optional[ui.AddContactDialog] = None
        self._settings_dialog: Optional[ui.SettingsDialog] = None

        # 6. Build UI layout (30% Sidebar / 70% ChatWindow)
        self._build_layout()

        # 7. Intercept window close for clean engine shutdown
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # 8. First-boot check or initialize engine
        if not self.settings.get("has_accepted_tc", False):
            self.after(100, self._show_tc_modal)
        else:
            self._init_engine_and_contacts()

    # -----------------------------------------------------------------------
    # Layout & UI Construction
    # -----------------------------------------------------------------------
    def _build_layout(self) -> None:
        """Create the 30/70 WhatsApp Web style grid."""
        # 30% left column (weight=3), 70% right column (weight=7)
        self.grid_columnconfigure(0, weight=3, minsize=280)
        self.grid_columnconfigure(1, weight=7, minsize=480)
        self.grid_rowconfigure(0, weight=1)

        # Sidebar (Left)
        self.sidebar = ui.Sidebar(
            self,
            fonts=self.fonts,
            resolve_avatar=self._resolve_avatar,
            on_select=self._on_contact_selected,
            on_add=self._open_add_contact,
            on_settings=self._open_settings,
        )
        self.sidebar.grid(row=0, column=0, sticky="nsew")

        # Chat Window (Right)
        self.chat_window = ui.ChatWindow(
            self,
            fonts=self.fonts,
            resolve_avatar=self._resolve_avatar,
            on_send=self._on_send_message,
            on_clear_chat=self._on_clear_chat_request,
            on_delete_contact=self._on_delete_contact_request,
        )
        self.chat_window.grid(row=0, column=1, sticky="nsew")

    def _resolve_avatar(self, contact: dict[str, Any]) -> Optional[Path]:
        """Helper to get absolute Path to avatar."""
        return self.data_manager.avatar_abs_path(contact)

    # -----------------------------------------------------------------------
    # First-Boot / EULA Modal
    # -----------------------------------------------------------------------
    def _show_tc_modal(self) -> None:
        """Display the blocking Terms & Conditions dialog."""
        ui.TermsConditionsDialog(
            self,
            fonts=self.fonts,
            on_accept=self._on_tc_accepted,
            on_decline=self._on_tc_declined,
        )

    def _on_tc_accepted(self) -> None:
        """Save acceptance to settings and load AI model."""
        now_iso = datetime.now().isoformat()
        self.settings = self.data_manager.update_settings(
            has_accepted_tc=True,
            accepted_tc_at=now_iso,
        )
        logger.info("Terms and Conditions accepted at %s", now_iso)
        self._init_engine_and_contacts()

    def _on_tc_declined(self) -> None:
        """User declined terms: exit the application immediately."""
        logger.info("Terms and Conditions declined. Exiting AlterChat.")
        self.destroy()
        sys.exit(0)

    # -----------------------------------------------------------------------
    # AI Engine & Contacts Initialization
    # -----------------------------------------------------------------------
    def _init_engine_and_contacts(self) -> None:
        """Load contacts and trigger background AI model loading."""
        self._refresh_contacts_list()
        self._start_model_loading()

    def _start_model_loading(self) -> None:
        """Initialize AI engine with custom API endpoint via ai_engine singleton."""
        self.sidebar.set_engine_status(ui.tr("model_loading"), ui.Palette.STATUS_WARN)
        engine = ai.get_engine()

        def on_done(success: bool, message: str) -> None:
            self.dispatcher.call(self._on_model_loaded, success, message)

        engine.load_model_async(on_done=on_done)

    def _on_model_loaded(self, success: bool, message: str) -> None:
        """Callback on main thread when model loader finishes."""
        if success:
            self.sidebar.set_engine_status(ui.tr("model_ready"), ui.Palette.STATUS_OK)
            if self._settings_dialog and ui.widget_alive(self._settings_dialog):
                self._settings_dialog.set_model_status(message)
        else:
            self.sidebar.set_engine_status(ui.tr("model_error"), ui.Palette.STATUS_ERR)
            if self._settings_dialog and ui.widget_alive(self._settings_dialog):
                self._settings_dialog.set_model_status(ui.tr("model_error_detail", error=message))

    # -----------------------------------------------------------------------
    # Contact Selection & Navigation
    # -----------------------------------------------------------------------
    def _refresh_contacts_list(self) -> None:
        """Load contacts from disk into the sidebar."""
        contacts = self.data_manager.load_contacts()
        self.sidebar.set_contacts(contacts)

    def _on_contact_selected(self, contact_id: str) -> None:
        """Switch active chat to the selected contact."""
        contact = self.data_manager.get_contact(contact_id)
        if not contact:
            return

        self.active_contact = contact
        self.sidebar.set_active(contact_id)

        # Load history and populate chat window
        history = self.data_manager.load_history(contact_id)
        self.chat_window.load_contact(contact, history)

    # -----------------------------------------------------------------------
    # Chat & Inference Flow (Threaded + File-based RAG)
    # -----------------------------------------------------------------------
    def _on_send_message(self, user_text: str) -> bool:
        """Handle user message submission: continuous save, RAG, background inference."""
        if not self.active_contact:
            return False

        contact = self.active_contact
        contact_id = contact["id"]

        engine = ai.get_engine()
        if not engine.is_ready:
            # Inform user model is not ready
            if engine.state == ai.EngineState.LOADING:
                self.chat_window.add_notice(ui.tr("model_still_loading"), error=False)
            elif engine.state == ai.EngineState.ERROR:
                self.chat_window.add_notice(
                    ui.tr("model_error_detail", error=engine.error), error=True
                )
            else:
                self.chat_window.add_notice(
                    ui.tr("model_error_detail", error="API Engine is not ready. Please verify CUSTOM_API_KEY in .env"),
                    error=True,
                )
            return False

        if engine.is_busy:
            self.chat_window.add_notice(ui.tr("status_busy_other"), error=False)
            return False

        # 1. Continuous Saving: Append user message to riwayat_[id].txt immediately
        ts = self.data_manager.append_history(contact_id, "user", user_text)

        # 2. Render user bubble in GUI
        self.chat_window.add_message("user", user_text, ts)
        self._refresh_contacts_list()
        self.sidebar.set_active(contact_id)

        # 3. Assemble prompt context (Persona + Keyword RAG + recent turns)
        def messages_factory() -> list[dict[str, str]]:
            return self.data_manager.build_prompt_messages(
                contact,
                user_text,
                history_turns=dm.HISTORY_CONTEXT_TURNS,
                rag_top_k=dm.RAG_TOP_K,
            )

        # 4. Initiate background streaming inference
        self._streaming_text = ""
        self._current_gen_contact_id = contact_id
        self.chat_window.set_status(ui.tr("status_typing"))
        self.chat_window.begin_reply()
        self.chat_window.set_send_enabled(False)

        max_tokens = int(self.settings.get("max_tokens", 200))
        temperature = float(self.settings.get("temperature", 0.8))

        engine.generate_async(
            messages_factory,
            persona_name=contact.get("name", ""),
            max_tokens=max_tokens,
            temperature=temperature,
            on_token=self._on_inference_token,
            on_reset=self._on_inference_reset,
            on_done=lambda text, cancelled: self.dispatcher.call(
                self._on_inference_done, contact_id, text, cancelled
            ),
            on_error=lambda err: self.dispatcher.call(
                self._on_inference_error, contact_id, err
            ),
        )
        return True

    def _on_inference_token(self, token: str) -> None:
        """Stream token to GUI via dispatcher."""
        self._streaming_text += token
        # Only update GUI if the user hasn't switched contacts
        if self.active_contact and self.active_contact["id"] == self._current_gen_contact_id:
            self.dispatcher.call(self.chat_window.update_reply, self._streaming_text)

    def _on_inference_reset(self) -> None:
        """Discard partial streamed text if an echo retry triggers."""
        self._streaming_text = ""
        if self.active_contact and self.active_contact["id"] == self._current_gen_contact_id:
            self.dispatcher.call(self.chat_window.update_reply, "")

    def _on_inference_done(self, contact_id: str, full_text: str, cancelled: bool) -> None:
        """Finalize reply: clean text, append to history, update UI."""
        self.chat_window.set_send_enabled(True)
        self.chat_window.set_status(ui.tr("status_idle"))

        if cancelled:
            if self.active_contact and self.active_contact["id"] == contact_id:
                self.chat_window.end_reply(None)
            return

        contact = self.data_manager.get_contact(contact_id)
        persona_name = contact["name"] if contact else "Friend"
        cleaned_reply = dm.clean_model_reply(full_text, persona_name)
        if not cleaned_reply:
            cleaned_reply = ui.tr("no_reply")

        # Save AI reply to riwayat_[id].txt
        ts = self.data_manager.append_history(contact_id, "assistant", cleaned_reply)

        # Update chat window if active
        if self.active_contact and self.active_contact["id"] == contact_id:
            self.chat_window.end_reply(cleaned_reply, ts)

        self._refresh_contacts_list()
        if self.active_contact:
            self.sidebar.set_active(self.active_contact["id"])

    def _on_inference_error(self, contact_id: str, error_msg: str) -> None:
        """Handle inference failure."""
        self.chat_window.set_send_enabled(True)
        self.chat_window.set_status(ui.tr("status_idle"))

        if self.active_contact and self.active_contact["id"] == contact_id:
            self.chat_window.end_reply(None)
            self.chat_window.add_notice(ui.tr("reply_error", error=error_msg), error=True)

    # -----------------------------------------------------------------------
    # Modals: Add Contact & Settings
    # -----------------------------------------------------------------------
    def _open_add_contact(self) -> None:
        """Open Add Contact dialog."""
        if self._add_dialog and ui.widget_alive(self._add_dialog):
            self._add_dialog.lift()
            self._add_dialog.focus_force()
            return

        self._add_dialog = ui.AddContactDialog(
            self,
            data_manager=self.data_manager,
            dispatcher=self.dispatcher,
            fonts=self.fonts,
            on_saved=self._on_contact_saved,
        )

    def _on_contact_saved(self, contact: dict[str, Any]) -> None:
        """Callback when contact creation completes."""
        self._refresh_contacts_list()
        self._on_contact_selected(contact["id"])

    def _open_settings(self) -> None:
        """Open Settings dialog."""
        if self._settings_dialog and ui.widget_alive(self._settings_dialog):
            self._settings_dialog.lift()
            self._settings_dialog.focus_force()
            return

        engine = ai.get_engine()
        status_text = (
            ui.tr("model_ready")
            if engine.is_ready
            else (
                ui.tr("model_loading")
                if engine.state == ai.EngineState.LOADING
                else (
                    ui.tr("model_error_detail", error=engine.error)
                    if engine.state == ai.EngineState.ERROR
                    else ui.tr("model_none")
                )
            )
        )

        self._settings_dialog = ui.SettingsDialog(
            self,
            settings=self.settings,
            fonts=self.fonts,
            model_status=status_text,
            on_language=self._on_language_change,
            on_theme=self._on_theme_change,
            on_model_selected=self._on_model_selected,
            on_clear_all=self._on_clear_all_request,
        )

    def _on_language_change(self, code: str) -> None:
        """Switch language, save setting, and re-render."""
        self.settings = self.data_manager.update_settings(language=code)
        ui.set_language(code)
        self._rebuild_ui()

    def _on_theme_change(self, mode: str) -> None:
        """Switch theme and save setting."""
        self.settings = self.data_manager.update_settings(theme=mode)
        ctk.set_appearance_mode(mode)

    def _on_model_selected(self, path: str) -> str:
        """Save settings and reload API engine."""
        self.settings = self.data_manager.update_settings(model_path=path)
        engine = ai.get_engine()
        engine.shutdown()

        self.sidebar.set_engine_status(ui.tr("model_loading"), ui.Palette.STATUS_WARN)

        def on_done(success: bool, msg: str) -> None:
            self.dispatcher.call(self._on_model_loaded, success, msg)

        engine.load_model_async(on_done=on_done)
        return ui.tr("model_loading_now")

    def _on_clear_all_request(self, parent_dialog: tk.Misc) -> None:
        """Confirm and clear all user data."""
        def confirmed() -> None:
            self.data_manager.clear_all_data()
            self.active_contact = None
            self._refresh_contacts_list()
            self.chat_window.show_empty()
            if self._settings_dialog and ui.widget_alive(self._settings_dialog):
                self._settings_dialog.close()

        ui.ConfirmDialog(
            parent_dialog,
            title=ui.tr("clear_all_confirm_title"),
            message=ui.tr("clear_all_confirm_msg"),
            confirm_label=ui.tr("clear_all"),
            cancel_label=ui.tr("cancel"),
            is_destructive=True,
            fonts=self.fonts,
            on_confirmed=confirmed,
        )

    # -----------------------------------------------------------------------
    # Chat Context Menu Actions (Clear Chat / Delete Contact)
    # -----------------------------------------------------------------------
    def _on_clear_chat_request(self) -> None:
        """Confirm and clear message history for current contact."""
        if not self.active_contact:
            return
        contact = self.active_contact
        contact_id = contact["id"]

        def confirmed() -> None:
            self.data_manager.clear_history(contact_id)
            self._refresh_contacts_list()
            self.sidebar.set_active(contact_id)
            self.chat_window.load_contact(contact, [])

        ui.ConfirmDialog(
            self,
            title=ui.tr("confirm_clear_chat_title"),
            message=ui.tr("confirm_clear_chat_msg", name=contact["name"]),
            confirm_label=ui.tr("menu_clear_chat"),
            cancel_label=ui.tr("cancel"),
            is_destructive=True,
            fonts=self.fonts,
            on_confirmed=confirmed,
        )

    def _on_delete_contact_request(self) -> None:
        """Confirm and delete the contact and all its data."""
        if not self.active_contact:
            return
        contact = self.active_contact
        contact_id = contact["id"]

        def confirmed() -> None:
            self.data_manager.delete_contact(contact_id)
            self.active_contact = None
            self._refresh_contacts_list()
            self.chat_window.show_empty()

        ui.ConfirmDialog(
            self,
            title=ui.tr("confirm_delete_title"),
            message=ui.tr("confirm_delete_msg", name=contact["name"]),
            confirm_label=ui.tr("menu_delete_contact"),
            cancel_label=ui.tr("cancel"),
            is_destructive=True,
            fonts=self.fonts,
            on_confirmed=confirmed,
        )

    # -----------------------------------------------------------------------
    # Rebuilding UI & Cleanup
    # -----------------------------------------------------------------------
    def _rebuild_ui(self) -> None:
        """Re-render main interface widgets when language changes."""
        self.title(ui.tr("app_title"))
        saved_active_id = self.active_contact["id"] if self.active_contact else None

        self.sidebar.destroy()
        self.chat_window.destroy()

        self._build_layout()
        self._refresh_contacts_list()

        engine = ai.get_engine()
        if engine.is_ready:
            self.sidebar.set_engine_status(ui.tr("model_ready"), ui.Palette.STATUS_OK)
        elif engine.state == ai.EngineState.LOADING:
            self.sidebar.set_engine_status(ui.tr("model_loading"), ui.Palette.STATUS_WARN)
        elif engine.state == ai.EngineState.ERROR:
            self.sidebar.set_engine_status(ui.tr("model_error"), ui.Palette.STATUS_ERR)
        else:
            self.sidebar.set_engine_status(ui.tr("model_missing"), ui.Palette.STATUS_WARN)

        if saved_active_id:
            self._on_contact_selected(saved_active_id)

    def _on_close(self) -> None:
        """Orderly shutdown of AI engine and dispatcher."""
        logger.info("AlterChat shutting down...")
        self.dispatcher.stop()
        ai.get_engine().shutdown()
        self.destroy()


def main() -> None:
    """Application entry point."""
    app = AlterChatApp()
    app.mainloop()


if __name__ == "__main__":
    main()

