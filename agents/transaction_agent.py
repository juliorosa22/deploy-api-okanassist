from agno.agent import Agent
import re
import json
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
import asyncio
from agno.models.groq import Groq
from tools import Transaction, TransactionType, SupabaseClient
from tools.pdf_generator import create_transaction_report_pdf # Import the new PDF generator
from messages import get_message
from agno.models.google import Gemini

class TransactionAgent:
    """Specialized agent for handling financial transactions"""

    def __init__(self, supabase_client: SupabaseClient):
        self.supabase_client = supabase_client
        
        self.expense_categories = ["Essentials", "Food & Dining", "Transportation", "Shopping", "Entertainment", "Utilities", "Healthcare", "Travel", "Education", "Home"]
        self.income_categories = ["Salary", "Freelance", "Business", "Investment", "Gift", "Refund", "Rental", "Other Income"]
        
        expense_cats = ", ".join(self.expense_categories)
        income_cats = ", ".join(self.income_categories)
        
        self.text_agent = Agent(
            name="TextTransactionProcessor",
            model=Groq(id="llama-3.3-70b-versatile", temperature=0.1),
            instructions=f"""
                You are a multilingual financial AI assistant. Your first task is to determine the user's intent from their message.

                **Step 1: Intent Detection**
                Determine the user's intent. Possible intents are:
                - `create_transaction`: The user wants to log a new expense or income. (e.g., "paid $50 for dinner", "got my $2000 salary")
                - `get_summary`: The user wants a text summary of their finances. (e.g., "show my spending last week", "summary for this month")
                - `generate_report`: The user wants a detailed PDF report. (e.g., "I need a report for the last 30 days", "generate pdf for january")

                **Step 2: Parameter Extraction**
                - If intent is `create_transaction`, extract: `amount`, `description`, `transaction_type`, `category`, `merchant`.
                  - The `category` MUST be one of the following.
                  - Expense Categories: {expense_cats}
                  - Income Categories: {income_cats}
                  - If the category is unclear, default to "Shopping" for expenses and "Other Income" for income.
                - If intent is `get_summary` or `generate_report`, extract date filters: `start_date` and `end_date` in 'YYYY-MM-DD' format.

                **Step 3: JSON Output**
                You MUST return ONLY a valid JSON object.

                **JSON Output Examples:**

                *Create Transaction:*
                `{{"intent": "create_transaction", "data": {{"amount": 50.00, "description": "Dinner", "transaction_type": "expense", "category": "Food & Dining"}}}}`

                *Get Summary:*
                `{{"intent": "get_summary", "filters": {{"start_date": "2025-09-01", "end_date": "2025-09-30"}}}}`

                *Generate Report:*
                `{{"intent": "generate_report", "filters": {{"start_date": "2025-01-01", "end_date": "2025-01-31"}}}}`
            """
        )
        
        # Initialize Gemini agent for vision processing (receipt/image analysis)
        # For bank statement document batch extraction
        self.vision_agent = Agent(
            model=Gemini(id="gemini-2.0-flash"),
            markdown=True,
            instructions=f"""
            You are a financial document processor.

            Your task is to analyze the attached bank statement PDF and extract every transaction as a batch.

            Expense Categories: {expense_cats}
            Income Categories: {income_cats}

            For each transaction, extract:
            - Amount (positive for income, negative or positive for expenses, but mark transaction type clearly)
            - Description (what the transaction is for)
            - Date (YYYY-MM-DD format)
            - Transaction type ("income" or "expense")
            - Category (must be from the lists above)

            Rules:
            - The category must be exactly one from the appropriate list.
            - If the category is unclear for an expense, use "Shopping".
            - If the category is unclear for an income, use "Other Income".
            - Only include transactions that are clearly present in the document.

            Output:
            Return ONLY a valid JSON array of transactions. Do not include any explanation or extra text.

            Example:
            [
            {{
                "amount": 100.00,
                "description": "Salary payment",
                "date": "2025-09-01",
                "transaction_type": "income",
                "category": "Salary"
            }},
            {{
                "amount": 25.50,
                "description": "Grocery shopping",
                "date": "2025-09-02",
                "transaction_type": "expense",
                "category": "Essentials"
            }}
            ]
            """
)

    #TODO adapt to respond in user's language
    async def process_message(self, user_data: Dict[str, Any], message: str) -> Dict[str, Any]:
        """
        Process a text message to determine user intent (create, summarize, or report)
        and execute the corresponding action. Returns a dict with response type and content.
        """
        lang = user_data.get('language', 'en')
        
        try:
            extraction_prompt = f"""
            The user's current date is {datetime.now().strftime('%Y-%m-%d')}.
            Analyze the following user message and return the JSON output based on your instructions.

            **User Message:** "{message}"
            """
            
            response_obj = await asyncio.to_thread(self.text_agent.run, extraction_prompt)
            response_str = response_obj.content
            print("🤖 Transaction Agent: LLM Response:", response_str)  # Debug log
            json_match = re.search(r'\{.*\}', response_str, re.DOTALL)
            if not json_match:
                raise ValueError("No JSON object found in LLM response.")
            parsed_response = json.loads(json_match.group())
            intent = parsed_response.get("intent")
            
            print(f"Detected intent: {intent}, parsed_response: {parsed_response}")  # Debug log
            if intent == "create_transaction":
                data = parsed_response.get("data", {})
                data["original_message"] = message  # Add original message for context
                return await self._handle_create_transaction(user_data, data)
            elif intent == "get_summary":
                filters = parsed_response.get("filters", {})
                filters["original_message"] = message  # Add original message for context
                return await self._handle_get_summary(user_data, filters)
            elif intent == "generate_report":
                filters = parsed_response.get("filters", {})
                filters["original_message"] = message  # Add original message for context
                return await self._handle_generate_report(user_data, filters)
            else:
                return {"type": "text", "content": get_message("unclear_transaction_intent", lang)}

        except Exception as e:
            print(f"❌ Transaction Agent: Error processing message: {e}")
            return {"type": "text", "content": get_message("generic_error", lang)}

    async def get_summary(self, user_data: Dict[str, Any], days: int) -> str:
        """
        Generates a financial summary for a given number of past days.
        This is a standalone method for a dedicated endpoint.
        """
        user_id = user_data.get('user_id')
        lang = user_data.get('language', 'en')
        try:
            # --- FIX: Use timedelta for date calculation ---
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days)
            print(start_date, end_date)
            summary = await self.supabase_client.database.get_transaction_summary(
                user_id, start_date, end_date
            )
            
            net_flow = summary.total_income - summary.total_expenses
            flow_emoji = "📈" if net_flow >= 0 else "📉"
            
            message = f"{flow_emoji} *Financial Summary (Last {days} days)*\n\n"
            message += f"💰 *Income:* ${summary.total_income:,.2f}\n"
            message += f"💸 *Expenses:* ${summary.total_expenses:,.2f}\n"
            message += f"📊 *Net Flow:* ${net_flow:,.2f}\n\n"
            
            if summary.expense_categories:
                message += "*Top Expense Categories:*\n"
                for cat in summary.expense_categories:
                    message += f"• {cat['category']}: ${cat['total']:,.2f}\n"
            
            return message
        except Exception as e:
            print(f"❌ Error generating summary: {e}")
            return get_message("generic_error", lang)

    async def _handle_create_transaction(self, user_data: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        """Handles the logic for creating a new transaction."""
        user_id = user_data.get('user_id')
        lang = user_data.get('language', 'en')
        print(f"Creating transaction for user {user_id} with data: {data}")
        if not data.get("amount") or not data.get("description"):
             return {"type": "text", "content": get_message("unclear_transaction_intent", lang)}

        # --- FIX: Use .get() with defaults for safety ---
        transaction_type_str = data.get("transaction_type", "expense") # Default to 'expense'
        category_str = data.get("category", "Shopping") # Default to 'Shopping'

        validated_category = self._validate_category(category_str, transaction_type_str)
        
        transaction = Transaction(
            user_id=user_id,
            amount=abs(float(data["amount"])),
            description=data["description"],
            category=validated_category,
            transaction_type=TransactionType(transaction_type_str), # Use the safe variable
            original_message=data.get("original_message", "N/A"),
            source_platform="telegram",
            merchant=data.get("merchant"),
            confidence_score=data.get("confidence", 0.9)
        )
        print("Saving transaction:", transaction)
        await self.supabase_client.database.save_transaction(transaction)
        
        emoji = "💸" if transaction_type_str == "expense" else "💰"
        message_template = get_message(
            "transaction_created", 
            lang, 
            emoji=emoji, 
            description=data['description'], 
            amount=data['amount'], 
            category=validated_category,
            transaction_type=transaction_type_str.title() # Pass type to message
        )
        print("Transaction created message:", message_template)
        return {"type": "text", "content": message_template}

    async def _handle_get_summary(self, user_data: Dict[str, Any], filters: Dict[str, Any]) -> Dict[str, Any]:
        """Handles fetching and formatting a transaction summary."""
        user_id = user_data.get('user_id')
        lang = user_data.get('language', 'en')
        
        start_date = datetime.fromisoformat(filters['start_date']) if filters.get('start_date') else None
        end_date = datetime.fromisoformat(filters['end_date']) if filters.get('end_date') else None

        summary = await self.supabase_client.database.get_transaction_summary(user_id, start_date, end_date)
        
        net_flow = summary.total_income - summary.total_expenses
        flow_emoji = "📈" if net_flow >= 0 else "📉"
        
        message = f"{flow_emoji} *Financial Summary ({summary.period_days} days)*\n\n"
        message += f"💰 *Income:* ${summary.total_income:,.2f}\n"
        message += f"💸 *Expenses:* ${summary.total_expenses:,.2f}\n"
        message += f"📊 *Net Flow:* ${net_flow:,.2f}\n\n"
        
        if summary.expense_categories:
            message += "*Top Expense Categories:*\n"
            for cat in summary.expense_categories:
                message += f"• {cat['category']}: ${cat['total']:,.2f}\n"
        
        return {"type": "text", "content": message}

    async def _handle_generate_report(self, user_data: Dict[str, Any], filters: Dict[str, Any]) -> Dict[str, Any]:
        """Handles generating a PDF transaction report."""
        user_id = user_data.get('user_id')
        lang = user_data.get('language', 'en')
        
        start_date = datetime.fromisoformat(filters['start_date']) if filters.get('start_date') else datetime.now() - timedelta(days=30)
        end_date = datetime.fromisoformat(filters['end_date']) if filters.get('end_date') else datetime.now()

        transactions = await self.supabase_client.database.get_user_transactions(user_id, start_date, end_date)
        
        if not transactions:
            return {"type": "text", "content": get_message("no_transactions_for_report", lang)}
            
        pdf_path = create_transaction_report_pdf(transactions, user_data.get('name', 'User'), start_date, end_date)
        
        # Return a special response type for the API to handle file sending
        return {
            "type": "file",
            "content": pdf_path,
            "caption": get_message("pdf_report_caption", lang, start_date=start_date.strftime('%Y-%m-%d'), end_date=end_date.strftime('%Y-%m-%d'))
        }

    async def process_receipt_image(self, user_data: Dict[str, Any], image_path: str, lang: str = 'en') -> str:
        """Process receipt image using Gemini vision capabilities"""

        try:
            #print("DEBUG: user_data type:", type(user_data), "value:", user_data)  # Debug log
            user_currency = user_data.get('currency', 'USD')
            user_id = user_data.get('user_id', None)
            print(f"Processing receipt image for user {user_id} at {image_path}")

            # Use Gemini vision to extract receipt data
            extraction_prompt = f"""
            You are a financial receipt processor.

            Analyze the attached receipt image and extract the transaction details:

            - Total amount paid (final amount)
            - Merchant or store name
            - Date of transaction (YYYY-MM-DD format if possible)
            - Category (choose exactly one from the defined categories
            - Any notable items purchased

            Rules:
            - The category must be exactly one from the provided list.
            - If you are unsure of the category, use "Shopping" as the default.
            - Only include information that is clearly present on the receipt.

            Output:
            Return ONLY a valid JSON object with the extracted data. Do not include any explanation or extra text.

            Example:
            {{
            "amount": 23.45,
            "description": "Purchase at Starbucks: Latte, Croissant",
            "merchant": "Starbucks",
            "date": "2025-09-20",
            "category": "Food & Dining",
            "transaction_type": "expense",
            "confidence": 0.9,
            }}
            """
           


            image_dict = {"filepath": image_path}
            response_obj = await asyncio.to_thread(
                self.vision_agent.run,  
                    extraction_prompt,
                    images=[image_dict]  # Try bytes instead of path
                )
                
           
            response = response_obj.content
        
            print("Raw response from Gemini Vision:", response)
            # Parse the response
            try:
                # Clean response to extract JSON
                json_start = response.find('{')
                json_end = response.rfind('}') + 1
                if json_start >= 0 and json_end > json_start:
                    json_str = response[json_start:json_end]
                    data = json.loads(json_str)
                    
                    # Validate that data is a dict
                    if not isinstance(data, dict):
                        print(f"❌ Parsed data is not a dict: {type(data)}, value: {data}")
                        return "📸 Receipt processed, but the extracted data was invalid. Please try again."
                else:
                    raise ValueError("No JSON found in response")
            except Exception as parse_e:
                print(f"❌ JSON parsing error: {parse_e}")
                return "📸 Receipt processed, but I had trouble extracting the data. Please manually enter the transaction."
            
            # Now safe to use data.get() since we validated it's a dict
            validated_category = self._validate_category(data.get("category", "Shopping"), "expense")
            
            # Create transaction from receipt data
            transaction = Transaction(
                user_id=user_id,
                amount=abs(float(data.get("amount", 0))),
                description=f"Purchase at {data.get('merchant', 'Store')}",
                category=validated_category,
                transaction_type=TransactionType.EXPENSE,
                original_message="Receipt upload",
                source_platform="telegram",
                merchant=data.get("merchant"),
                confidence_score=data.get("confidence", 0.85),
                receipt_image_url=image_path  # Store the path
            )
            
            # Save to database
            saved_transaction = await self.supabase_client.database.save_transaction(transaction)
            
            return (
                get_message(
                    "success_process_receipt", 
                    lang,
                    merchant=data.get("merchant", "Store"),
                    amount=data.get("amount", 0.0),
                    category=validated_category,
                    date=datetime.now().strftime("%Y-%m-%d")
                )
            )
            
        except Exception as e:
            print(f"❌ Transaction (Receipt): Error processing receipt image: {e}")
            return "❌ Sorry, I couldn't process that receipt image. Please try again or enter the transaction manually."

    async def process_bank_statement(self, user_data: Dict[str, Any], pdf_path: str, lang: str = 'en') -> str:
        """Process bank statement PDF using Gemini"""
        user_id = user_data.get('user_id', None)
        user_currency = user_data.get('currency', 'USD')

        try:
            # Use Gemini to extract multiple transactions from PDF
            extraction_prompt = f"""
            You are a financial document processor.

            1. Detect the user's language from the document.
            2. The currency for all transactions is: {user_currency}.

            Analyze the attached bank statement PDF and extract every transaction.

            Expense Categories (choose one for each expense): {self.expense_categories}
            Income Categories (choose one for each income): {self.income_categories}

            For each transaction, extract:
            - Amount (positive for income, negative or positive for expenses, but mark transaction type clearly)
            - Description (what the transaction is for)
            - Date (YYYY-MM-DD format)
            - Transaction type ("income" or "expense")
            - Category (must be from the lists above)

            Rules:
            - The category must be exactly one from the appropriate list.
            - If the category is unclear for an expense, use "Shopping".
            - If the category is unclear for an income, use "Other Income".

            Output:
            Return ONLY a valid JSON array of transactions. Do not include any explanation or extra text.
            Example:
            [
              {{
                "amount": 100.00,
                "description": "Salary payment",
                "date": "2025-09-01",
                "transaction_type": "income",
                "category": "Salary",
                "confidence_score": 0.85
              }},
              {{
                "amount": 25.50,
                "description": "Grocery shopping",
                "date": "2025-09-02",
                "transaction_type": "expense",
                "category": "Essentials",
                "confidence_score": 0.9
              }}
            ]
            """
            pdf_dict = {"filepath": pdf_path}
            response_obj = await asyncio.to_thread(
                self.vision_agent.run,
                extraction_prompt,
                files=[pdf_dict]
            )
            response = response_obj.content # <-- FIX: Access the .content attribute
            
            # Parse transactions
            try:
                # Clean response to extract JSON array
                json_start = response.find('[')
                json_end = response.rfind(']') + 1
                if json_start >= 0 and json_end > json_start:
                    json_str = response[json_start:json_end]
                    transactions_data = json.loads(json_str)
                else:
                    # Try to find JSON object instead
                    json_start = response.find('{')
                    json_end = response.rfind('}') + 1
                    if json_start >= 0 and json_end > json_start:
                        json_str = response[json_start:json_end]
                        single_transaction = json.loads(json_str)
                        transactions_data = [single_transaction]
                    else:
                        raise ValueError("No JSON found in response")
            except:
                return "📄 PDF processed, but I had trouble extracting transaction data. Please check the file format."
            
            # Save each transaction
            saved_count = 0
            for trans_data in transactions_data:
                try:
                    # Validate category
                    validated_category = self._validate_category(
                        trans_data.get("category", "Shopping"), 
                        trans_data.get("transaction_type", "expense")
                    )
                    
                    transaction = Transaction(
                        user_id=user_id,
                        amount=abs(float(trans_data.get("amount", 0))),
                        description=trans_data.get("description", "Bank transaction"),
                        category=validated_category,
                        transaction_type=TransactionType(trans_data.get("transaction_type", "expense")),
                        original_message="Bank statement import",
                        source_platform="telegram",
                        confidence_score=0.85,
                        tags=["bank_import"]
                    )
                    
                    await self.supabase_client.database.save_transaction(transaction)
                    saved_count += 1
                except Exception as e:
                    print(f"❌ Error saving transaction: {e}")
                    continue

            return get_message("success_process_pdf", lang, saved_count=saved_count)

        except Exception as e:
            print(f"❌ Transaction (Bank Statement): Error processing bank statement: {e}")
            return "❌ Sorry, I couldn't process that bank statement. Please ensure it's a valid PDF with transaction data."
    
    #TODO adapt to respond in user's language
    async def get_summary(self, user_data: Dict[str, Any], days: int = 30) -> str:
        """Generate financial summary using Groq for insights"""
        user_id = user_data.get('user_id', None)
        lang = user_data.get('language', 'en')
        try:
            # Get summary data from database
            summary = await self.supabase_client.database.get_transaction_summary(user_id, days)
            print(f"Summary data: {summary}")
            # Use Groq for fast insights generation
            insights_prompt = f"""
            Analyze this financial summary and provide brief insights:
            
            - Total Income: ${summary.total_income:.2f}
            - Total Expenses: ${summary.total_expenses:.2f}
            - Income Transactions: {summary.income_count}
            - Expense Transactions: {summary.expense_count}
            - Top Categories: {summary.expense_categories}
            
            Provide 2-3 brief insights about spending patterns and financial health.
            Keep it concise and encouraging.
            """
            
            insights_obj = await asyncio.to_thread(self.text_agent.run, insights_prompt)
            insights = insights_obj.content # <-- FIX: Access the .content attribute
            
            # Calculate net flow
            net_flow = summary.total_income - summary.total_expenses
            flow_emoji = "📈" if net_flow > 0 else "📉" if net_flow < 0 else "📊"
            
            # Build summary message
            message = f"""
            {flow_emoji} *Financial Summary (Last {days} days)*

            💰 *Income:* ${summary.total_income:,.2f} ({summary.income_count} transactions)
            💸 *Expenses:* ${summary.total_expenses:,.2f} ({summary.expense_count} transactions)
            📊 *Net Flow:* ${net_flow:,.2f}

            *Top Expense Categories:*
            """
            
            for cat in summary.expense_categories[:3]:
                message += f"• {cat['category']}: ${cat['total']:,.2f}\n"
            
            #message += f"\n*AI Insights:*\n{insights}\n\n🚀 *Analysis by:* Groq Llama 3.1 70B"
            
            return message
            
        except Exception as e:
            print(f"❌ Error generating summary: {e}")
            return "❌ Sorry, I couldn't generate your financial summary right now. Please try again later."
    

    #Tools

    def _validate_category(self, category: str, transaction_type: str) -> str:
        """Validate category against predefined lists"""
        if transaction_type == "expense":
            if category in self.expense_categories:
                return category
            # Default for invalid expense categories
            return "Shopping"
        else:  # income
            if category in self.income_categories:
                return category
            # Default for invalid income categories
            return "Other Income"
    
    def _fallback_parse(self, message: str) -> Dict[str, Any]:
        """Fallback parsing when Agno doesn't return JSON"""
        # Extract amount
        amount_match = re.search(r'\$?(\d+(?:,\d{3})*(?:\.\d{2})?)', message)
        if not amount_match:
            return {"transaction_found": False}
        
        amount = float(amount_match.group(1).replace(',', ''))
        
        # Determine transaction type
        expense_words = ['spent', 'paid', 'bought', 'cost', 'expense']
        income_words = ['earned', 'received', 'income', 'salary', 'bonus']
        
        message_lower = message.lower()
        if any(word in message_lower for word in expense_words):
            transaction_type = "expense"
            category = "Shopping"  # Default expense category
        elif any(word in message_lower for word in income_words):
            transaction_type = "income"
            category = "Other Income"  # Default income category
        else:
            # Default based on context clues
            transaction_type = "expense"  # Most common
            category = "Shopping"
        
        # Clean description
        description = re.sub(r'\$?[\d,]+\.?\d*', '', message).strip()
        description = re.sub(r'\b(spent|paid|bought|for|on|at)\b', '', description, flags=re.IGNORECASE).strip()
        
        if not description:
            description = f"Transaction for ${amount:.2f}"
        
        return {
            "amount": amount,
            "description": description,
            "transaction_type": transaction_type,
            "category": category,
            "merchant": None,
            "confidence": 0.7
        }