"""Bot HITL de Telegram (spec v2.0, seccion 3.6).

Canal de aprobacion humana. Mensaje HTML con score, expectancy, R/R, entry/SL/TP,
p_win y tamanyo de posicion. Botones: Aprobar / Rechazar / Editar precios / Pausar 1h.
El flujo de edicion solicita entry, SL y TP uno a uno y muestra un resumen antes de
confirmar. TTL de 5 min: si no hay respuesta -> no operar (principio "no-operar por
defecto").

Se define la interfaz `HITLClient`, un `MockHITLClient` (decision programable, util
para tests y shadow mode) y `TelegramHITLClient` (cliente real con python-telegram-bot).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from src.schemas.decision import Decision
from src.schemas.hitl import ApprovalDecision, ApprovalRequest, ApprovalResponse, EditedPrices
from src.schemas.opportunity import Opportunity

if TYPE_CHECKING:  # pragma: no cover - solo para tipado
    from telegram import Update
    from telegram.ext import ContextTypes

logger = logging.getLogger("agentic.connectors.telegram")

# Mapea el prefijo del callback_data del boton a la decision HITL.
_ACTION_TO_DECISION = {
    "edit": None,  # no es una decision final; abre el flujo de edicion
    "approve": ApprovalDecision.APPROVE,
    "reject": ApprovalDecision.REJECT,
    "pause": ApprovalDecision.PAUSE_1H,
}

# Secuencia de campos que el operador edita uno a uno.
_EDIT_FIELDS = ("entry_price", "stop_loss", "take_profit")
_EDIT_LABELS = {"entry_price": "Entrada", "stop_loss": "Stop-Loss", "take_profit": "Take-Profit"}


@dataclass
class _EditState:
    """Estado de una sub-conversacion de edicion de precios para una peticion HITL."""

    request_id: str
    chat_id: int
    request: ApprovalRequest
    values: dict = field(default_factory=dict)  # campo -> valor ya introducido
    step: int = 0  # indice en _EDIT_FIELDS del campo que se esta pidiendo ahora


def build_request(decision: Decision, opp: Opportunity, ttl_seconds: int = 300) -> ApprovalRequest:
    """Compone el ApprovalRequest a partir de la decision y la oportunidad."""
    return ApprovalRequest(
        opportunity_id=opp.opportunity_id,
        ticker=opp.ticker,
        summary=opp.justification,
        final_score=decision.final_score,
        expectancy_pct=decision.expectancy_pct,
        risk_reward_ratio=decision.risk_reward_ratio,
        entry_price=opp.entry_price,
        stop_loss=opp.stop_loss,
        take_profit=opp.take_profit,
        estimated_win_probability=opp.estimated_win_probability,
        position_size_pct=opp.position_size_pct,
        evaluator_breakdown=decision.evaluator_breakdown,
        ttl_seconds=ttl_seconds,
    )


def _geometry_error(
    entry_price: float, stop_loss: float, take_profit: float, is_long: bool
) -> str | None:
    """Valida la geometria de precios editados; devuelve un mensaje de error o None si es OK.

    Coincide con `Opportunity._validate_bracket_geometry`: LONG exige
    stop_loss < entrada < take_profit; SHORT exige take_profit < entrada < stop_loss. Evita
    confirmar ordenes con geometria incoherente (p. ej. un stop por encima de la entrada en
    una compra), que el broker rechazaria o dejaria en un estado inconsistente.
    """
    if is_long:
        if not (stop_loss < entry_price < take_profit):
            return "En LONG debe cumplirse: Stop-Loss &lt; Entrada &lt; Take-Profit."
    else:
        if not (take_profit < entry_price < stop_loss):
            return "En SHORT debe cumplirse: Take-Profit &lt; Entrada &lt; Stop-Loss."
    return None


class HITLClient(Protocol):
    async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse: ...


class MockHITLClient:
    """HITL simulado. La decision se fija o se calcula con un callback opcional.

    Por defecto devuelve TIMEOUT (no operar), respetando el principio de la spec.
    """

    def __init__(
        self,
        default_decision: ApprovalDecision = ApprovalDecision.TIMEOUT,
        decision_fn: Callable[[ApprovalRequest], ApprovalDecision] | None = None,
        responder: str = "mock_user",
        edited_prices: "EditedPrices | None" = None,
    ) -> None:
        self.default_decision = default_decision
        self.decision_fn = decision_fn
        self.responder = responder
        self.edited_prices = edited_prices
        self.requests: list[ApprovalRequest] = []

    async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        self.requests.append(request)
        decision = (
            self.decision_fn(request) if self.decision_fn else self.default_decision
        )
        return ApprovalResponse(
            request_id=request.request_id,
            opportunity_id=request.opportunity_id,
            decision=decision,
            responder=self.responder,
            edited_prices=self.edited_prices if decision == ApprovalDecision.APPROVE else None,
        )


class TelegramHITLClient:
    """Cliente HITL real con python-telegram-bot (v21, asincrono).

    Envia un mensaje con teclado inline (Aprobar / Rechazar / Pausar 1h) a cada usuario
    autorizado y espera la respuesta con TTL. Si expira -> ``TIMEOUT`` (no operar). Solo
    resuelven la peticion los usuarios en ``authorized_users``.

    Ciclo de vida: llama a ``start()`` antes de usar (arranca el polling) y a ``stop()``
    al cerrar. El envio/espera se hace en ``request_approval``.
    """

    def __init__(self, bot_token: str, authorized_users: list[int]) -> None:
        try:  # pragma: no cover - depende del extra opcional
            import telegram  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "python-telegram-bot no esta instalado. Instala: pip install -e .[connectors]"
            ) from exc
        self.bot_token = bot_token
        self.authorized_users = set(authorized_users)
        self._app = None  # telegram.ext.Application, creado en start()
        self._pending: dict[str, asyncio.Future] = {}
        # message_ids enviados por peticion: request_id -> list[(chat_id, message_id)]
        self._sent_messages: dict[str, list[tuple[int, int]]] = {}
        # ApprovalRequest activa por request_id (necesaria para la sub-conversacion de edicion)
        self._pending_requests: dict[str, ApprovalRequest] = {}
        # Estado de edicion activa: chat_id -> _EditState (un chat edita a la vez)
        self._edit_states: dict[int, _EditState] = {}

    async def start(self) -> None:  # pragma: no cover - requiere Telegram
        """Arranca la Application de Telegram y el polling de callbacks."""
        from telegram.ext import Application as TgApp
        from telegram.ext import CallbackQueryHandler, MessageHandler, filters

        self._app = TgApp.builder().token(self.bot_token).build()
        self._app.add_handler(CallbackQueryHandler(self._on_callback))
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        logger.info("Bot de Telegram iniciado (%d usuarios autorizados).", len(self.authorized_users))

    async def stop(self) -> None:  # pragma: no cover - requiere Telegram
        """Detiene el polling y cierra la Application ordenadamente."""
        if self._app is None:
            return
        try:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        finally:
            self._app = None
        logger.info("Bot de Telegram detenido.")

    def _keyboard(self, request_id: str):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Aprobar", callback_data=f"approve:{request_id}"),
                    InlineKeyboardButton("❌ Rechazar", callback_data=f"reject:{request_id}"),
                ],
                [
                    InlineKeyboardButton("✏️ Editar precios", callback_data=f"edit:{request_id}"),
                    InlineKeyboardButton("⏸ Pausar 1h", callback_data=f"pause:{request_id}"),
                ],
            ]
        )

    def _keyboard_confirm(self, request_id: str):
        """Teclado de confirmacion tras editar precios."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Aprobar con estos precios", callback_data=f"approve:{request_id}"),
                    InlineKeyboardButton("❌ Rechazar", callback_data=f"reject:{request_id}"),
                ]
            ]
        )

    @staticmethod
    def _format(request: ApprovalRequest) -> str:
        return (
            f"<b>{request.ticker}</b> — score {request.final_score:.2f} | p_win {request.estimated_win_probability:.0%}\n"
            f"Entrada {request.entry_price} | SL {request.stop_loss} | TP {request.take_profit}\n"
            f"Expectancy {request.expectancy_pct:.2f}% | R/R {request.risk_reward_ratio:.2f} | Tamaño {request.position_size_pct:.1%}\n"
            f"{request.summary}"
        )

    @staticmethod
    def _format_edited(request: ApprovalRequest, edited: dict) -> str:
        entry = edited.get("entry_price", request.entry_price)
        sl = edited.get("stop_loss", request.stop_loss)
        tp = edited.get("take_profit", request.take_profit)
        return (
            f"<b>{request.ticker}</b> — score {request.final_score:.2f} | p_win {request.estimated_win_probability:.0%}\n"
            f"Entrada <b>{entry}</b> | SL <b>{sl}</b> | TP <b>{tp}</b> <i>(editado)</i>\n"
            f"Expectancy {request.expectancy_pct:.2f}% | R/R {request.risk_reward_ratio:.2f} | Tamaño {request.position_size_pct:.1%}\n"
            f"{request.summary}"
        )

    async def _on_message(  # pragma: no cover - requiere Telegram
        self, update: "Update", context: "ContextTypes.DEFAULT_TYPE"
    ) -> None:
        """Recibe los valores numericos durante la sub-conversacion de edicion de precios."""
        user = update.effective_user
        user_id = user.id if user else None
        if user_id not in self.authorized_users:
            return
        state = self._edit_states.get(user_id)
        if state is None:
            return  # no hay edicion activa para este usuario

        text = (update.message.text or "").strip().replace(",", ".")
        field_name = _EDIT_FIELDS[state.step]
        try:
            value = float(text)
            if value <= 0:
                raise ValueError("debe ser > 0")
        except ValueError:
            await update.message.reply_text(
                f"⚠️ Valor invalido. Introduce un numero positivo para {_EDIT_LABELS[field_name]}:"
            )
            return

        state.values[field_name] = value
        state.step += 1

        if state.step < len(_EDIT_FIELDS):
            next_field = _EDIT_FIELDS[state.step]
            current_val = getattr(state.request, next_field)
            await update.message.reply_text(
                f"Introduce {_EDIT_LABELS[next_field]} (actual: {current_val}):"
            )
        else:
            # Todos los campos recibidos -> validar geometria antes de confirmar. La
            # direccion se infiere de la geometria original (siempre coherente).
            is_long = state.request.take_profit > state.request.entry_price
            geo_err = _geometry_error(
                state.values["entry_price"], state.values["stop_loss"],
                state.values["take_profit"], is_long,
            )
            if geo_err is not None:
                # Reiniciar la edicion: el operador reintroduce desde el primer campo.
                state.values.clear()
                state.step = 0
                first_field = _EDIT_FIELDS[0]
                await update.message.reply_text(
                    f"⚠️ {geo_err}\nVuelve a introducir {_EDIT_LABELS[first_field]} "
                    f"(actual: {getattr(state.request, first_field)}):",
                    parse_mode="HTML",
                )
                return
            # Geometria valida -> mostrar resumen y pedir confirmacion.
            del self._edit_states[user_id]
            summary = (
                f"✏️ <b>Precios editados para {state.request.ticker}:</b>\n"
                f"Entrada: {state.values['entry_price']}\n"
                f"Stop-Loss: {state.values['stop_loss']}\n"
                f"Take-Profit: {state.values['take_profit']}\n\n"
                f"Confirma o rechaza la operacion con los nuevos precios:"
            )
            # Guardar los precios editados en el estado de la peticion para que
            # _on_callback los recupere al recibir approve/reject.
            self._edit_states[-(user_id)] = state  # clave negativa = esperando confirmacion
            await update.message.reply_text(
                summary,
                parse_mode="HTML",
                reply_markup=self._keyboard_confirm(state.request_id),
            )

    async def _on_callback(  # pragma: no cover - requiere Telegram
        self, update: "Update", context: "ContextTypes.DEFAULT_TYPE"
    ) -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        user = update.effective_user
        user_id = user.id if user else None
        if user_id not in self.authorized_users:
            logger.warning("Callback de usuario no autorizado (%s) ignorado.", user_id)
            await query.edit_message_text("No autorizado.")
            return
        action, _, request_id = (query.data or "").partition(":")

        # -- Boton Editar: iniciar sub-conversacion --
        if action == "edit":
            req = next(
                (r for r in [self._pending.get(request_id)] if r is not None), None
            )
            # Recuperar la ApprovalRequest desde los metadatos almacenados
            stored_request = self._pending_requests.get(request_id)
            if stored_request is None:
                await query.answer("Peticion no encontrada o ya expirada.", show_alert=True)
                return
            self._edit_states[user_id] = _EditState(
                request_id=request_id,
                chat_id=query.message.chat_id,
                request=stored_request,
            )
            first_field = _EDIT_FIELDS[0]
            current_val = getattr(stored_request, first_field)
            await query.message.reply_text(
                f"✏️ <b>Edicion de precios para {stored_request.ticker}</b>\n"
                f"Introduce {_EDIT_LABELS[first_field]} (actual: {current_val}):",
                parse_mode="HTML",
            )
            return

        if action not in _ACTION_TO_DECISION or _ACTION_TO_DECISION[action] is None:
            return
        decision = _ACTION_TO_DECISION[action]
        who = user.username or str(user_id)

        # Recuperar precios editados si existen (clave negativa = edicion completada)
        edit_state = self._edit_states.pop(-(user_id), None)
        edited_prices: EditedPrices | None = None
        if edit_state is not None and edit_state.request_id == request_id:
            try:
                edited_prices = EditedPrices(
                    entry_price=edit_state.values["entry_price"],
                    stop_loss=edit_state.values["stop_loss"],
                    take_profit=edit_state.values["take_profit"],
                )
            except (KeyError, ValueError) as exc:
                logger.warning("precios editados invalidos, se ignoran: %s", exc)

        fut = self._pending.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result((decision, who, edited_prices))
        # Editar todos los mensajes enviados para esta peticion (no solo el del respondedor)
        # para que los demas usuarios vean el resultado y no puedan seguir pulsando.
        result_text = f"→ {action.upper()} por {who}"
        if edited_prices:
            result_text += (
                f" (precios editados: entrada={edited_prices.entry_price} "
                f"SL={edited_prices.stop_loss} TP={edited_prices.take_profit})"
            )
        for chat_id, msg_id in self._sent_messages.pop(request_id, []):
            try:
                original = query.message.text if chat_id == query.message.chat_id else ""
                await self._app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=f"{original}\n\n{result_text}" if original else result_text,
                )
            except Exception as exc:  # noqa: BLE001 - no abortar si un chat falla
                logger.debug("No se pudo editar mensaje %s/%s: %s", chat_id, msg_id, exc)

    async def request_approval(  # pragma: no cover - requiere Telegram
        self, request: ApprovalRequest
    ) -> ApprovalResponse:
        if self._app is None:
            raise RuntimeError("TelegramHITLClient.start() no ha sido invocado.")
        request_id = str(request.request_id)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[request_id] = fut
        self._pending_requests[request_id] = request

        text = self._format(request)
        keyboard = self._keyboard(request_id)
        sent = 0
        sent_msgs: list[tuple[int, int]] = []
        for uid in self.authorized_users:
            try:
                msg = await self._app.bot.send_message(
                    uid, text, parse_mode="HTML", reply_markup=keyboard
                )
                sent_msgs.append((uid, msg.message_id))
                sent += 1
            except Exception as exc:  # noqa: BLE001 - no abortar por un chat fallido
                logger.warning("No se pudo enviar aprobacion a %s: %s", uid, exc)
        self._sent_messages[request_id] = sent_msgs

        responder: str | None = None
        edited_prices: EditedPrices | None = None
        try:
            if sent == 0:
                raise asyncio.TimeoutError
            result = await asyncio.wait_for(fut, timeout=request.ttl_seconds)
            decision, responder, edited_prices = result
        except asyncio.TimeoutError:
            decision = ApprovalDecision.TIMEOUT
            responder = None
            logger.info("Aprobacion %s expirada (TTL %ds) -> no operar.", request_id, request.ttl_seconds)
        finally:
            self._pending.pop(request_id, None)
            self._sent_messages.pop(request_id, None)
            self._pending_requests.pop(request_id, None)

        return ApprovalResponse(
            request_id=request.request_id,
            opportunity_id=request.opportunity_id,
            decision=decision,
            responder=responder,
            edited_prices=edited_prices,
        )
