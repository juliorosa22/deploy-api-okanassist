from agno.agent import Agent
import re
from typing import Dict, Any
import asyncio  # <-- 1. IMPORT ASYNCIO
from agno.models.groq import Groq
from messages import  MESSAGES, get_message
# Fix: Use package imports
from tools import SupabaseClient
from agno.models.google import Gemini
from agno.media import Audio
class MainAgent:
    """Main agent that handles user management and routes messages to specialized agents"""

    def __init__(self, supabase_client: SupabaseClient):
        self.supabase_client = supabase_client
        
        # Initialize Agno agent for intent classification
        self.agent = Agent(
            name="MainAssistant",
            model=Groq(id="llama-3.3-70b-versatile", temperature=0.2),
            instructions="""
            You are the OkanAssist main agent coordinator. Your role is to classify a user's message into a high-level category.

            Classify user messages into ONE of these categories:
            - TRANSACTION: For anything related to money, expenses, income, summaries, or financial reports. (e.g., "spent $20", "show my spending", "I need a PDF report for this month").
            - REMINDER: For anything related to creating reminders, tasks, events, or asking for a schedule. (e.g., "remind me to call mom", "what's on my agenda today?").
            - HELP: For help requests or questions about functionality.
            - GREETING: For simple greetings and casual conversation.
            
            Respond with ONLY the classification category in uppercase (e.g., "TRANSACTION", "REMINDER").
            """
        )
        # Add Gemini agent for audio transcription
        self.audio_agent = Agent(
            name="AudioTranscriber",
            model=Gemini(id="gemini-2.0-flash"),
            instructions="""
            You are the OkanAssist speech to text main agent. Your role is to:
            process user audio input in any language and transcribe it to English text.

            1. Identify the user's spoken language from the audio.
            2. Transcribe the audio to English text only.
            3. Return ONLY the English transcript. Do not include any explanation, language name, or extra text.

            Example output:
            "Pay rent tomorrow at 10am."
            "Schedule a meeting with John next Monday."
            "I spent $15 on groceries."
            "What's my balance?"
            4. If the audio is unclear or cannot be transcribed, respond with:
            "I'm sorry, I couldn't understand the audio. Please try again."
            """
        )

    async def route_message(self, user_data: Dict[str, Any], message: str) -> str:
        """Route user message to appropriate agent based on intent - NO AUTH CHECK"""
        lang_map = {'es': 'Spanish', 'pt': 'Portuguese', 'en': 'English'}
        lang = user_data.get('language', 'en')
        lang_name = lang_map.get(lang.split('-')[0], 'English')
        user_timezone = user_data.get('timezone', 'UTC')
        user_id = user_data.get('id')
        try:
            # User is already authenticated at this point (checked in API layer)
           
            # --- 2. RUN THE BLOCKING CALL IN A SEPARATE THREAD ---
            intent_response_obj = await asyncio.to_thread(
                self.agent.run,
                f"The user is speaking {lang_name}. Classify this user message and explain briefly: '{message}'"
            )
            intent_response = str(intent_response_obj.content)
            print("Intent response main agent:", intent_response)
            
            # Route based on intent classification
            if self._contains_intent(intent_response, "TRANSACTION"):
                from .transaction_agent import TransactionAgent
                transaction_agent = TransactionAgent(self.supabase_client)
                return await transaction_agent.process_message(user_data, message)
                
            elif self._contains_intent(intent_response, "REMINDER"):
                from .reminder_agent import ReminderAgent
                reminder_agent = ReminderAgent(self.supabase_client)
                return await reminder_agent.process_message(user_data, message)

                
            elif self._contains_intent(intent_response, "HELP"):
                # Use the help message as context for a generated response
                user_name = user_data.get('name', 'there')
                help_context = self._get_help_content(lang)
                
                help_prompt = f"""
                You are a friendly and helpful assistant named OkanAssist. The user, {user_name}, has asked for help.
                Your task is to provide a helpful and engaging response based on the context below. Do not just repeat the text, but explain the main features in a conversational way.

                - **Always respond in the user's language** ({lang_name}).
                - Keep the response concise and friendly.
                - Use emojis to make it more engaging.

                **Help Context to use:**
                ---
                {help_context}
                ---
                """
                
                help_response_obj = await asyncio.to_thread(
                    self.agent.run,
                    help_prompt
                )
                help_response = str(help_response_obj.content)
                return {'type': 'text', 'content': help_response}

            else:
                # General conversation
                # --- 3. APPLY THE FIX HERE AS WELL ---
                user_name = user_data.get('name', 'there')
                general_prompt = f"""
                The intent is {intent_response}. Respond helpfully and engagingly to this message: '{message}'.
                - ** Respond mentioning the user name : {user_name}**
                - **Always respond in the user's language** ({lang_name}). If the language is unclear, default to English.
                - **Suggest how they can use OkanAssistant Bot**: Tracking transactions through simple messages, audios and receipts. Tracking daily activities and tasks with reminders.
                - **OkanFit company goal** : Remember that OkanFit company mission: is to develop innovative solutions built on a robust AI stack, always focusing on meeting real user needs. Guided by the principle of Occam's Razor, we strive for simplicity and efficiency in every product we create. 
                - When reasonable, add a fun, light-hearted tone with emojis or playful phrases to keep it enjoyable (e.g., for greetings or casual chats).
                - Also, encourage them to follow OkanFit on instagram: @okanfit.dev (https://www.instagram.com/okanfit.dev) and visit https://www.okanfit.dev.br for more tips and updates.
                - Keep responses concise and avoid long replies.
                """
                general_response_obj = await asyncio.to_thread(
                    self.agent.run,
                    general_prompt
                )
                general_response = str(general_response_obj.content)
                dict_response = { 'type':'text','content': general_response }
                return dict_response
                
        except Exception as e:
            #print("failed to route message")
            print(f"❌ Main Agent: Error routing message: {e}")
            return "❌ Sorry, I encountered an error. Please try rephrasing your request."

    async def route_audio(self, user_data: Dict[str, Any], audio_path: str) -> str:
        """Transcribe audio to English and route to the correct agent."""
        try:
            # Step 1: Transcribe audio to English using Gemini
            #audio_dict = {"filepath": audio_path}
            #print("Routing audio for transcription:", audio_dict)
            user_id = user_data.get('user_id')
            print("Routing audio for transcription:", audio_path)
            response_obj = await asyncio.to_thread(
                self.audio_agent.run,
                "Identify the user's language from the audio, then transcribe the audio to English. Return ONLY the English transcript.",
                audio=[Audio(filepath=audio_path)]
            )
            transcript = str(response_obj.content).strip()
            print("Transcribed audio (English):", transcript)

            # Step 2: Route the transcript as a message
            return await self.route_message(user_data, transcript)

        except Exception as e:
            print(f"❌ Main Agent: Error routing audio: {e}")
            return "❌ Sorry, I couldn't process your audio. Please try again or use text input."

    def _get_help_content(self, lang: str = 'en') -> str:
        """Return help content without authentication"""
        return get_message("help_message", lang)

    def _contains_intent(self, response: str, intent: str) -> bool:
        """Check if the response contains the specified intent"""
        return intent.lower() in response.lower()
    
    #TODO expand keybord lists to handle similar words in different languages
    async def classify_intent(self, message: str) -> str:
        """Classify message intent using simple keyword matching as fallback"""
        message_lower = message.lower()
        
        # Transaction keywords
        transaction_keywords = [
            'spent', 'paid', 'bought', 'purchase', 'cost', 'expense', 'income', 
            'earned', 'received', 'salary', 'freelance', '$', 'dollar', 'money'
        ]
        
        # Reminder keywords
        reminder_keywords = [
            'remind', 'reminder', 'remember', 'don\'t forget', 'schedule', 
            'appointment', 'meeting', 'call', 'pay', 'tomorrow', 'later'
        ]
        
        # Summary keywords
        summary_keywords = [
            'balance', 'summary', 'total', 'show', 'view', 'expenses', 
            'spending', 'income', 'report', 'this month', 'this week'
        ]
        
        if any(keyword in message_lower for keyword in transaction_keywords):
            return "TRANSACTION"
        elif any(keyword in message_lower for keyword in reminder_keywords):
            return "REMINDER"
        elif any(keyword in message_lower for keyword in summary_keywords):
            return "SUMMARY"
        else:
            return "GENERAL"