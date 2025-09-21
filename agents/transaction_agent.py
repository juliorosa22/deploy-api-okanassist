from agno.agent import Agent
import re
import json
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
import asyncio  # <-- 1. IMPORT ASYNCIO
from agno.models.groq import Groq
# Fix: Use package imports from __init__.py
from tools import Transaction, TransactionType, SupabaseClient
from messages import  MESSAGES, get_message
from agno.models.google import Gemini

class TransactionAgent:
    """Specialized agent for handling financial transactions"""

    def __init__(self, supabase_client: SupabaseClient):
        self.supabase_client = supabase_client
        
        # Define simplified categories
        self.expense_categories = ["Essentials", "Food & Dining", "Transportation", "Shopping", "Entertainment", "Utilities", "Healthcare", "Travel", "Education", "Home"]
        self.income_categories = ["Salary", "Freelance", "Business", "Investment", "Gift", "Refund", "Rental", "Other Income"]
        
        # Build category strings for agent instructions
        expense_cats = ", ".join(self.expense_categories)
        income_cats = ", ".join(self.income_categories)
        
        # Initialize Groq agent for text processing (cost-effective)
        self.text_agent = Agent(
            name="TextTransactionProcessor",
            model=Groq(id="llama-3.3-70b-versatile", temperature=0.1),  # Lower temp for stricter JSON
            instructions=f"""
                You are a precise financial data extractor. Your task is to analyze a user's message and extract transaction details into a strict JSON format.

                **Step-by-Step Thought Process:**
                1.  **Identify Intent:** First, determine if the message describes an **expense** (money going out) or an **income** (money coming in).
                    *   **Expense Keywords:** Look for words like 'spent', 'paid', 'bought', 'cost', 'charged', 'bill'. If no keywords are present, most transactions are expenses by default (e.g., "uber ride $25").
                    *   **Income Keywords:** Look for words like 'earned', 'received', 'got paid', 'salary', 'bonus', 'refund', 'deposit'.
                2.  **Select Category List:** Based on the intent, select the appropriate category list.
                    *   **Expense Categories:** {expense_cats}
                    *   **Income Categories:** {income_cats}
                3.  **Extract Data:** Pull all required fields and strictly adhere to the output format.

                **Rules & Output Format:**
                - Return ONLY a valid JSON object. No explanations or surrounding text.
                - The `transaction_type` MUST be either "expense" or "income".
                - The `category` MUST be one of the exact strings from the chosen list.
                - If you are unsure of the category, use "Shopping" for expenses or "Other Income" for income.
                - If no clear transaction is found, return `{{"transaction_found": false}}`.

                **Examples:**
                - User: "just paid 20 bucks for my spotify sub" -> {{"amount": 20.00, "description": "Spotify subscription", "transaction_type": "expense", "category": "Entertainment", "merchant": "Spotify", "confidence": 0.95, "transaction_found": true}}
                - User: "got a $50 refund from amazon" -> {{"amount": 50.00, "description": "Refund from Amazon", "transaction_type": "income", "category": "Refund", "merchant": "Amazon", "confidence": 0.9, "transaction_found": true}}
                - User: "salary deposit $3000" -> {{"amount": 3000.00, "description": "Salary deposit", "transaction_type": "income", "category": "Salary", "merchant": null, "confidence": 1.0, "transaction_found": true}}
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
    async def process_message(self, user_id: str, message: str, lang: str) -> str:
        """Process a text message for transaction data using Groq"""
        
        lang_map = {'es': 'Spanish', 'pt': 'Portuguese', 'en': 'English'}
        lang_name = lang_map.get(lang.split('-')[0], 'English')
        try:
            # The agent now has all instructions. Just pass the user's message.
            extraction_prompt = f"""
             The user is speaking {lang_name}. Analyze the following user message and return the JSON output.
            Respond in {lang_name} for the confirmation message.

            **User Message:** "{message}"

            **JSON Output:**
            """
            
            response_obj = await asyncio.to_thread(self.text_agent.run, extraction_prompt)
            response = response_obj.content # <-- FIX: Access the .content attribute
            #print("Raw response from Groq:", response)
            # Enhanced JSON parsing for Groq responses
            try:
                # Clean response to extract JSON
                json_start = response.find('{')
                json_end = response.rfind('}') + 1
                if json_start >= 0 and json_end > json_start:
                    json_str = response[json_start:json_end]
                    data = json.loads(json_str)
                else:
                    raise ValueError("No JSON found in response")
            except:
                # Fallback parsing if JSON isn't returned
                data = self._fallback_parse(message)
            
            if not data.get("transaction_found", True):
                return "🤔 I couldn't find transaction information in your message. Try something like 'Spent $25 on groceries' or 'Received $500 salary'."
            
            # Validate and fix category
            validated_category = self._validate_category(data["category"], data["transaction_type"])
            data["category"] = validated_category
            #print("Extracted data:", data)
            # Create and save transaction
            transaction = Transaction(
                user_id=user_id,
                amount=abs(float(data["amount"])),
                description=data["description"],
                category=data["category"],
                transaction_type=TransactionType(data["transaction_type"]),
                original_message=message,
                source_platform="telegram",
                merchant=data.get("merchant"),
                confidence_score=data.get("confidence", 0.85),
                tags=[]
            )
            
            # Save to database
            saved_transaction = await self.supabase_client.database.save_transaction(transaction)
            
            # Generate response
            emoji = "💸" if data["transaction_type"] == "expense" else "💰"
            #TODO adapt to respond in user's language

            message_template = get_message("transaction_created", lang, emoji=emoji, description=data['description'], amount=data['amount'], category=data['category'], transaction_type=data['transaction_type'])
            return message_template
                
            
        except Exception as e:
            print(f"❌ Transaction Agent: Error processing transaction message: {e}")
            return "❌ Sorry, I couldn't process that transaction. Please try again with a clearer format."

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
    async def get_summary(self, user_id: str, days: int = 30, lang: str = 'en') -> str:
        """Generate financial summary using Groq for insights"""
        try:
            # Get summary data from database
            summary = await self.supabase_client.database.get_transaction_summary(user_id, days)
            
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