from agno.agent import Agent
import re
import json
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Tuple
import asyncio
from agno.models.groq import Groq
from tools import Reminder, ReminderType, Priority, SupabaseClient
from messages import get_message
import pytz # <-- 1. Import pytz

class ReminderAgent:
    """Specialized agent for handling reminders and tasks"""

    def __init__(self, supabase_client: SupabaseClient):
        self.supabase_client = supabase_client

        self.agent = Agent(
            name="ReminderProcessor",
            model=Groq(id="llama-3.3-70b-versatile", temperature=0.2),
            instructions="""
            You are a multilingual AI assistant specializing in task and reminder management. Your primary goal is to understand a user's intent from their message and respond accordingly. You will be given the user's current date and time to accurately interpret their requests.

            **Step 1: Intent Detection**
            First, determine the user's intent. The possible intents are:
            - `create_reminder`: The user wants to create a new reminder. (e.g., "remind me to call mom tomorrow", "meeting in 30 mins", "pay bills next week")
            - `get_reminders`: The user wants to see a list of their existing reminders. (e.g., "show my tasks for this week", "what are my urgent tasks tomorrow?")
            - `complete_reminders`: The user wants to mark reminders as done. (e.g., "complete today's tasks", "clear yesterday's reminders", "I finished this week's tasks", "clear all reminders")

            **Step 2: Parameter Extraction**
            - If the intent is `create_reminder`, extract: `title`, `description`, `due_datetime` (in UTC ISO 8601 format), `priority`, `reminder_type`, `is_recurring`, and `recurrence_pattern`.
            - If the intent is `get_reminders` or `complete_reminders`, infer date filters from the user's message. Extract: `priority` ("urgent", "high", "medium", "low"), `start_date` (in 'YYYY-MM-DD' format), and `end_date` (in 'YYYY-MM-DD' format).
            - If the user says "clear all reminders", the intent is `complete_reminders` but `start_date` and `end_date` should be `null`.

            **Step 3: JSON Output**
            You MUST return ONLY a valid JSON object based on the detected intent.

            **JSON Output Examples (Current Date: 2025-10-07):**

            *For `create_reminder` intent:*
            ```json
            {
                "intent": "create_reminder",
                "data": {
                    "title": "Call Mom",
                    "description": "Remember to call Mom to check in tomorrow.",
                    "due_datetime": "2025-10-08T15:00:00Z",
                    "priority": "high",
                    "reminder_type": "habit",
                    "is_recurring": false,
                    "recurrence_pattern": null
                }
            }
            ```
            *For `create_reminder` intent: (e.g., "Appointment with Dr. Smith Nov 20 at 3pm"):*
            ```json
            {
                "intent": "create_reminder",
                "data": {
                    "title": "Doctor Appointment",
                    "description": "Don't forget your appointment with Dr. Smith on November 20 at 3pm.",
                    "due_datetime": "2025-11-20T15:00:00Z",
                    "priority": "high",
                    "reminder_type": "event",
                    "is_recurring": false,
                    "recurrence_pattern": null
                }
            }
            ```
            *For `create_reminder` intent: (e.g., "daily workout at 6pm"):*
            ```json
            {
                "intent": "create_reminder",
                "data": {
                    "title": "Daily Workout",
                    "description": "Your daily workout session.",
                    "due_datetime": "2025-10-07T18:00:00Z",
                    "priority": "medium",
                    "reminder_type": "habit",
                    "is_recurring": true,
                    "recurrence_pattern": "daily"
                }
            }
            ```

            *For `get_reminders` intent (e.g., "show me this week's tasks"):*
            ```json
            {
                "intent": "get_reminders",
                "filters": {
                    "priority": null,
                    "start_date": "2025-10-06",
                    "end_date": "2025-10-12"
                }
            }
            ```

            *For `complete_reminders` intent (e.g., "complete yesterday's tasks"):*
            ```json
            {
                "intent": "complete_reminders",
                "filters": {
                    "start_date": "2025-10-06",
                    "end_date": "2025-10-06"
                }
            }
            ```

            *For `complete_reminders` intent (e.g., "clear all reminders"):*
            ```json
            {
                "intent": "complete_reminders",
                "filters": {
                    "start_date": null,
                    "end_date": null
                }
            }
            ```

            *If no clear intent is found:*
            ```json
            {
                "intent": "unclear"
            }
            ```
            """
        )

    async def process_message(self, user_data: Dict[str, Any], message: str) -> str:
        """
        Process a text message to determine user intent (create or get reminders)
        and execute the corresponding action.
        """
        language = user_data.get('language', 'en')
        user_timezone = user_data.get('timezone', 'UTC')
        
        try:
            user_tz = pytz.timezone(user_timezone)
        except pytz.UnknownTimeZoneError:
            user_tz = pytz.utc
        user_now = datetime.now(user_tz)

        extraction_prompt = f"""
        The user's current date and time is {user_now.isoformat()}.
        Analyze the following user message and return the JSON output based on your instructions.

        **User Message:** "{message}"
        """
        
        response_obj = await asyncio.to_thread(self.agent.run, extraction_prompt)
        response_str = str(response_obj.content)
        print(f"🤖 Reminder  LLM Response: {response_str}")

        try:
            json_match = re.search(r'\{.*\}', response_str, re.DOTALL)
            if not json_match:
                raise ValueError("No JSON object found in LLM response.")
            parsed_response = json.loads(json_match.group())
            intent = parsed_response.get("intent")
        except (json.JSONDecodeError, ValueError) as e:
            print(f"⚠️ Could not parse intent from LLM response: {e}")
            return {"type": "text", "content": get_message("reminder_creation_failed", language)}

        if intent == "create_reminder":
            return await self._handle_create_reminder(user_data, parsed_response.get("data", {}))
        elif intent == "get_reminders":
            return await self._handle_get_reminders(user_data, parsed_response.get("filters", {}))
        elif intent == "complete_reminders":
            return await self._handle_complete_reminders(user_data, parsed_response.get("filters", {}))
        else:
            return {"type": "text", "content": get_message("reminder_not_found", language)}

    async def _handle_create_reminder(self, user_data: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        """Handles the logic for creating a new reminder."""
        user_id = user_data.get('user_id')
        language = user_data.get('language', 'en')
        user_timezone = user_data.get('timezone', 'UTC')
        user_tz = pytz.timezone(user_timezone)

        if not data.get("title"):
            return get_message("reminder_creation_failed", language)

        due_datetime_utc = None
        if data.get("due_datetime"):
            # LLM returns UTC, so parse it and make it naive for DB storage
            due_datetime_utc = self._parse_due_date(data.get("due_datetime")).replace(tzinfo=None)

        due_datetime_response = data.get("due_datetime")
        #print(f"🗓️ Parsed due date (UTC): {due_datetime_response} -> {due_datetime_utc}")

        reminder = Reminder(
            user_id=user_id,
            title=data.get("title"),
            description=data.get("description", data.get("title")),
            source_platform="telegram",
            is_completed=False,
            notification_sent=False,
            due_datetime=due_datetime_utc,
            reminder_type=ReminderType(data.get("reminder_type", "general")),
            priority=Priority(data.get("priority", "medium")),
            is_recurring=data.get("is_recurring", False),
            recurrence_pattern=data.get("recurrence_pattern")
        )
        
        await self.supabase_client.database.save_reminder(reminder)
        
        display_due_date = "N/A"
        if due_datetime_utc:
            local_due_date = pytz.utc.localize(due_datetime_utc).astimezone(user_tz)
            display_due_date = local_due_date.strftime('%Y-%m-%d %H:%M')
        reminder_msg=get_message(
            "reminder_created",
            language,
            title=data['title'],
            due_date=display_due_date,
            priority=data.get('priority', 'medium').title(),
            type=data.get('reminder_type', 'general').title()
        )
        return {"type": "text", "content": reminder_msg}

    async def _handle_complete_reminders(self, user_data: Dict[str, Any], filters: Dict[str, Any]) -> Dict[str, Any]:
        """Handles the logic for completing reminders based on filters."""
        user_id = user_data.get('user_id')
        language = user_data.get('language', 'en')

        start_date_str = filters.get("start_date")
        end_date_str = filters.get("end_date")

        # Handle "clear all" case
        if not start_date_str and not end_date_str:
            completed_count = await self.supabase_client.database.mark_all_reminders_complete(user_id)
            period_msg = get_message("period_all", language)
        else:
            try:
                # Parse dates and set time to start/end of day
                start_date = datetime.fromisoformat(start_date_str).replace(hour=0, minute=0, second=0, microsecond=0)
                end_date = datetime.fromisoformat(end_date_str).replace(hour=23, minute=59, second=59, microsecond=999999)
                period_msg = f"{start_date_str} to {end_date_str}"
            except (ValueError, TypeError):
                return {"type": "text", "content": get_message("unclear_intent", language)}

            completed_count = await self.supabase_client.database.mark_reminders_complete_by_date(
                user_id, start_date, end_date
            )

        if completed_count > 0:
            message = get_message("reminders_completed", language, count=completed_count, period=period_msg)
        else:
            message = get_message("no_reminders_to_complete", language, period=period_msg)
        
        return {"type": "text", "content": message}

    async def _handle_get_reminders(self, user_data: Dict[str, Any], filters: Dict[str, Any]) -> Dict[str, Any]:
        """Handles the logic for fetching and formatting reminders based on filters."""
        user_id = user_data.get('user_id')
        language = user_data.get('language', 'en')

        priority = filters.get("priority")
        start_date_str = filters.get("start_date")
        end_date_str = filters.get("end_date")

        try:
            start_date = datetime.fromisoformat(start_date_str).replace(hour=0, minute=0, second=0, microsecond=0) if start_date_str else None
            end_date = datetime.fromisoformat(end_date_str).replace(hour=23, minute=59, second=59, microsecond=999999) if end_date_str else None
        except (ValueError, TypeError):
            return {"type": "text", "content": get_message("unclear_intent", language)}

        reminders = await self.supabase_client.database.get_user_reminders(
            user_id, 
            priority=priority,
            start_date=start_date,
            end_date=end_date
        )
        
        if not reminders:
            return {"type": "text", "content": get_message("no_pending_reminders", language)}

        formatted_string = await self.format_reminders_for_display(user_data, reminders)
        return {"type": "text", "content": formatted_string}

    async def get_reminders(self, user_data: Dict[str, Any], limit: int = 10) -> Dict[str, Any]:
        """Get user's latest pending reminders, formatted for display."""
        language = user_data.get('language', 'en')
        reminders = await self.supabase_client.database.get_user_reminders(
            user_data.get('user_id'), include_completed=False, limit=limit
        )
        if not reminders:
            return {"type": "text", "content": get_message("no_pending_reminders", language)}
        
        formatted_string = await self.format_reminders_for_display(user_data, reminders)
        return {"type": "text", "content": formatted_string}

    async def format_reminders_for_display(self, user_data: Dict[str, Any], reminders: List[Reminder]) -> str:
        """Takes a list of reminders and formats them into a human-readable string."""
        language = user_data.get('language', 'en')
        user_timezone = user_data.get('timezone', 'UTC')
        
        try:
            user_tz = pytz.timezone(user_timezone)
        except pytz.UnknownTimeZoneError:
            user_tz = pytz.utc
        user_now_iso = datetime.now(user_tz).isoformat()

        reminders_data = []
        for reminder in reminders:
            reminders_data.append({
                "title": reminder.title,
                "due": reminder.due_datetime.isoformat() if reminder.due_datetime else None,
                "priority": reminder.priority.value,
                "type": reminder.reminder_type.value
            })
        
        format_prompts = {
            "en": f"""
            The user's current time is {user_now_iso}. Format this list of reminders into a clean, readable message for Telegram. Do NOT output JSON—return plain text with emojis, line breaks, and Markdown formatting (e.g., bold for sections).

            Group reminders by priority: Urgent first, then High, Medium, Low. For each group, list reminders as bullet points like: "- [Emoji] Title (due [relative date]) - Type"

            Use these emojis:
            - Priority: 🔥 Urgent, ❗ High, 📌 Medium, 📝 Low
            - Type: 🕐 Task, 📅 Event, ⏰ Deadline, 🔄 Habit, 📝 General

            Example output:
            🔥 Urgent Reminders:
            - 📅 Call Mom (due today at 3:00 PM) - Habit

            ❗ High Priority:
            - ⏰ Pay Rent (due tomorrow) - Deadline

            Reminders data: {json.dumps(reminders_data)}
            """,
            "es": f"""
            La hora actual del usuario es {user_now_iso}. Formatea esta lista de recordatorios en un mensaje limpio y legible para Telegram. NO salidas JSON—devuelve texto plano con emojis, saltos de línea y formato Markdown (ej. negrita para secciones).

            Agrupa los recordatorios por prioridad: Urgente primero, luego Alta, Media, Baja. Para cada grupo, lista los recordatorios como viñetas como: "- [Emoji] Título (vencimiento [fecha relativa]) - Tipo"

            Usa estos emojis:
            - Prioridad: 🔥 Urgente, ❗ Alta, 📌 Media, 📝 Baja
            - Tipo: 🕐 Tarea, 📅 Evento, ⏰ Fecha límite, 🔄 Hábito, 📝 General

            Ejemplo de salida:
            🔥 Recordatorios Urgentes:
            - 📅 Llamar a Mamá (vencimiento hoy a las 3:00 PM) - Hábito

            ❗ Prioridad Alta:
            - ⏰ Pagar Renta (vencimiento mañana) - Fecha límite

            Datos de recordatorios: {json.dumps(reminders_data)}
            """,
            "pt": f"""
            A hora atual do usuário é {user_now_iso}. Formate esta lista de lembretes em uma mensagem limpa e legível para Telegram. NÃO saia JSON—retorne texto plano com emojis, quebras de linha e formatação Markdown (ex. negrito para seções).

            Agrupe os lembretes por prioridade: Urgente primeiro, depois Alta, Média, Baixa. Para cada grupo, liste os lembretes como marcadores como: "- [Emoji] Título (vencimento [data relativa]) - Tipo"

            Use estes emojis:
            - Prioridade: 🔥 Urgente, ❗ Alta, 📌 Média, 📝 Baixa
            - Tipo: 🕐 Tarefa, 📅 Evento, ⏰ Prazo, 🔄 Hábito, 📝 Geral

            Exemplo de saída:
            🔥 Lembretes Urgentes:
            - 📅 Ligar para Mamãe (vencimento hoje às 3:00 PM) - Hábito

            ❗ Prioridade Alta:
            - ⏰ Pagar Aluguel (vencimento amanhã) - Prazo

            Dados de lembretes: {json.dumps(reminders_data)}
            """
        }
        
        format_prompt = format_prompts.get(language, format_prompts["en"])
        # This part is simplified as the prompts are large and unchanged
        format_prompt = format_prompt.replace("Reminders data: {}", f"Reminders data: {json.dumps(reminders_data)}")

        response = await asyncio.to_thread(self.agent.run, format_prompt)
        formatted_list = str(response.content)
        
        return f"{get_message('pending_reminders_header', language)}\n\n{formatted_list}"

    async def get_due_soon(self, user_id: str, hours: int = 24) -> str:
        """Get reminders due soon"""
        try:
            due_reminders = await self.supabase_client.database.get_due_reminders(user_id, hours)
            
            if not due_reminders:
                return "✅ *No urgent reminders!*\n\nYou're on top of things! 🎯"
            
            message = f"⏰ *Reminders due in the next {hours} hours:*\n\n"
            
            for reminder in due_reminders:
                time_until = ""
                if reminder.due_datetime:
                    delta = reminder.due_datetime - datetime.now()
                    if delta.total_seconds() < 3600:  # Less than 1 hour
                        time_until = "⚡ Due very soon!"
                    elif delta.days == 0:
                        time_until = f"🕐 Due at {reminder.due_datetime.strftime('%I:%M %p')}"
                    else:
                        time_until = f"📅 Due {reminder.due_datetime.strftime('%m/%d at %I:%M %p')}"
                
                priority_emoji = {
                    "urgent": "🔥",
                    "high": "❗", 
                    "medium": "📌",
                    "low": "📝"
                }.get(reminder.priority.value, "📌")
                
                message += f"{priority_emoji} {reminder.title}\n{time_until}\n\n"
            
            return message
            
        except Exception as e:
            print(f"❌ Error getting due reminders: {e}")
            return "❌ Sorry, I couldn't check your due reminders right now."
    
    def _parse_due_date(self, date_str: str) -> Optional[datetime]:
        """Parse an ISO 8601 date/time string."""
        if not date_str:
            return None
        try:
            # The LLM is prompted to return ISO format, which is language-neutral
            return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        except (ValueError, TypeError):
            # Basic fallback for non-ISO formats (less reliable)
            print(f"⚠️ Could not parse date '{date_str}' with ISO format. Fallback may be inaccurate.")
            return None

    def _fallback_parse(self, message: str, language: str) -> Dict[str, Any]:
        """Fallback parsing when LLM doesn't return JSON, now with language support."""
        message_lower = message.lower()
        
        indicators = {
            "en": ['remind', 'remember', 'don\'t forget', 'schedule', 'appointment','meeting','task'],
            "es": ['recuérdame', 'recuerda', 'no olvides', 'agenda', 'cita'],
            "pt": ['lembre-me', 'lembre', 'não se esqueça', 'agende', 'compromisso','evento','marque','tarefa']
        }
        
        if not any(indicator in message_lower for indicator in indicators.get(language, indicators["en"])):
            return {"reminder_found": False}
        
        # This part remains basic, as it's a last resort.
        # A more advanced implementation would have language-specific keyword matching for priority/type.
        title = message
        priority = "medium"
        if "urgent" in message_lower or "urgente" in message_lower:
            priority = "urgent"
        
        return {
            "title": title,
            "priority": priority,
            "reminder_type": "general",
        }