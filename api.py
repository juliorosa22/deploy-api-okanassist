from fastapi import FastAPI, HTTPException, File, UploadFile, BackgroundTasks, Request, Form
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from typing import Optional, List, Dict, Any, Tuple
import uvicorn
import os
import tempfile
from datetime import datetime
from dotenv import load_dotenv
import aiohttp
import pytz  # Ensure pytz is imported at the top
# Import standardized messages
from messages import MESSAGES, get_message
import subprocess
# Import models
from models import (
    MessageRequest,
    TransactionResponse,
    SummaryRequest,
    StartRequest,
    NotificationRequest,
    RegisterRequest,
    AuthCheckRequest,
    UpgradeRequest
)

# Load environment
load_dotenv()

# Import existing components
from tools.supabase_tools import SupabaseClient
from agents.transaction_agent import TransactionAgent
from agents.reminder_agent import ReminderAgent
from agents.main_agent import MainAgent
from agents.timezone_agent import TimezoneAgent
from tools.session_manager import SessionManager

# Global services (initialized on-demand for GCF)
supabase_client = None
transaction_agent = None
reminder_agent = None
main_agent = None
timezone_agent = None
session_manager = None
bot_token = None
async def initialize_services():
    """Initialize services on-demand (for GCF compatibility)"""
    global supabase_client, transaction_agent, reminder_agent, main_agent, timezone_agent, session_manager, bot_token

    if supabase_client is None:
        print("🚀 Initializing API services...")
        
        # Initialize Supabase client
        supabase_url = os.getenv('SUPABASE_URL')
        supabase_key = os.getenv('SUPABASE_SECRET_KEY')
        bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        if not supabase_url or not supabase_key:
            raise ValueError("SUPABASE_URL and SUPABASE_SECRET_KEY are required")
        
        supabase_client = SupabaseClient(supabase_url, supabase_key)
        await supabase_client.connect()
        
        # Initialize agents
        transaction_agent = TransactionAgent(supabase_client)
        reminder_agent = ReminderAgent(supabase_client)
        main_agent = MainAgent(supabase_client)
        timezone_agent = TimezoneAgent()
        
        # Initialize session manager
        session_manager = SessionManager(session_timeout_minutes=30)
        
        print("✅ API services initialized successfully")

# Create FastAPI app (no lifespan for GCF)
app = FastAPI(
    title="OkanFit Assist AI API",
    description="Financial AI processing service",
    version="1.0.0"
)

# Add CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Endpoints
@app.get("/okanassist/v1/auth/confirm", response_class=JSONResponse)
async def handle_email_confirmation():
    """
    Simplified for GCF: Return JSON instead of HTML.
    """
    download_url = os.getenv("APP_DOWNLOAD_URL", "https://play.google.com/store/apps/details?id=com.okanassist")
    return {"message": "Registration confirmed! Download the app here.", "download_url": download_url}

@app.post("/okanassist/v1/start")
async def handle_start(request: StartRequest):
    """Handle /start command with authentication handling"""
    await initialize_services()  # Ensure services are ready
    lang = request.language_code
    try:
        if not main_agent:
            raise HTTPException(status_code=503, detail="Service not ready")
        
        # Use centralized authentication
        auth_request = AuthCheckRequest(
            telegram_id=request.telegram_id,
            user_name=request.user_data.get("name", None),
            supabase_user_id=request.args[0] if request.args else None,
            language=lang,
            timezone=request.args[2] if len(request.args) > 2 else "UTC",
            currency=request.args[3] if len(request.args) > 3 else "USD"
        )
        
        user_data = await get_user_data(auth_request)
        
        # If get_user_data succeeds, the user is authenticated
        name = user_data.get("name", request.user_data.get("name", "there"))
        return {
            "success": True,
            "message": get_message("welcome_authenticated", lang, name=name)
        }
        
    except HTTPException as e:
        print(f"HTTPException in handle_start: {e.detail}")
        return {
            "success": True,
            "message": get_message("welcome_unauthenticated", lang)
        }
    except Exception as e:
        print(f"❌ Unhandled Exception in handle_start: {e}")
        raise HTTPException(status_code=500, detail="An internal server error occurred.")

@app.get("/okanassist/v1/help")
async def handle_help(language_code: Optional[str] = 'en'):
    """Handle /help command - No authentication required"""
    return {"success": True, "message": get_message("help_message", language_code)}

@app.post("/okanassist/v1/upgrade")
async def handle_upgrade(request: UpgradeRequest):
    """Handles the premium upgrade request and generates a payment link."""
    await initialize_services()
    try:
        # 1. Authenticate the user and get their data
        auth_request = AuthCheckRequest(telegram_id=request.telegram_id)
        user_data = await get_user_data(auth_request)
        #return {"success": False, "message": "Upgrades are currently disabled. Please contact support."}
        # 2. Check if the user is already premium
        if user_data.get("is_premium"):
            return {
                "success": False,
                "message": get_message("already_premium", user_data.get("language", "en"))
            }

        # 3. Generate the payment link
        payment_details = await supabase_client.create_upgrade_link(user_data)

        if not payment_details.get("success"):
            raise HTTPException(status_code=500, detail="Could not generate payment link.")

        # 4. Format the response message for the user
        message = get_message("upgrade_to_premium", user_data.get("language", "en"),
            name=user_data.get("name", "there"),
            stripe_url=payment_details["stripe_url"]
        )
        
        return {"success": True, "message": message}

    except HTTPException as e:
        if e.status_code == 401:
            raise
        return {"success": False, "message": e.detail}
    except Exception as e:
        print(f"❌ Error in handle_upgrade: {e}")
        raise HTTPException(status_code=500, detail="An internal error occurred while processing your upgrade request.")
###
##TODO improve the webhook to handle refunds and cancellations
@app.post("/okanassist/v1/webhooks/stripe")
async def handle_stripe_webhook(request: Request):
    """Handles incoming webhooks from Stripe to confirm payments."""
    await initialize_services()
    if not supabase_client:
        raise HTTPException(status_code=503, detail="Service not ready")

    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')

    if not sig_header:
        raise HTTPException(status_code=400, detail="Missing Stripe-Signature header")

    try:
        result = await supabase_client.handle_stripe_webhook(payload, sig_header)
        success = result.get("success", False)
        telegram_id = result.get("telegram_id", None)
        message = result.get("message", "")
        print(f"Webhook processed: success={success}, telegram_id={telegram_id}, message={message}")
        
        if success and telegram_id:
            await send_telegram_message(telegram_id, message) if message else None
            user_data = await supabase_client.get_user_by_telegram_id_auth(telegram_id)
            if user_data:
                session_manager.create_session(telegram_id, user_data)
                print(f"✅ Session refreshed for user {telegram_id} after payment.")

            return JSONResponse(content={"status": "success"}, status_code=200)
        elif success:
            return JSONResponse(content={"status": "success"}, status_code=200)
        else:
            return JSONResponse(content={"status": "failed"}, status_code=400)

    except Exception as e:
        print(f"❌ Error processing Stripe webhook: {e}")
        return JSONResponse(content={"status": "error"}, status_code=500)

@app.post("/okanassist/v1/route-message")
async def route_message(request: MessageRequest):
    """Route message through main agent - REQUIRES AUTHENTICATION + CREDITS"""
    await initialize_services()
    lang = request.language_code
    try:
        if not main_agent:
            raise HTTPException(status_code=503, detail="Service not ready")
        
        # Step 1: Get user data using centralized helper
        user_data = await get_user_data(AuthCheckRequest(telegram_id=request.telegram_id))
        supabase_id = user_data.get('user_id', None)
        
        # Step 2: Consume credits
        credit_result = await check_and_consume_credits(supabase_id, 'text_message', 1, user_data)
        if not credit_result["success"]:
            return {"success": False, "message": credit_result.get("message")}

        user_data.setdefault('language', lang)
        
        # Step 3: Process the message
        agent_response = await main_agent.route_message(user_data, request.message)
        
        response_type = agent_response.get("type")
        response_content = agent_response.get("content")

        # Handle text responses
        if response_type == "text":
            final_message = response_content
            # Add credit info to response if not premium
            if not credit_result.get('is_premium', False):
                credits_remaining = credit_result.get('credits_remaining', 0)
                if credits_remaining <= 10:
                    final_message += get_message("credit_warning", lang, credits_remaining=credits_remaining)
            
            return {"success": True, "message": final_message}
        
        # Handle file responses (for PDF reports)
        elif response_type == "file":
            file_path = response_content
            caption = agent_response.get("caption", "Here is your report.")
            
            await send_telegram_document(request.telegram_id, file_path, caption)
            
            # Clean up the temporary file
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"⚠️ Failed to remove temporary file {file_path}: {e}")
            
            # Send a confirmation text message
            return {"success": True, "message": get_message("report_sent", lang)}
            
        else:
            # Fallback for unexpected response types
            return {"success": False, "message": "An unexpected error occurred."}
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Unexpected error in route_message: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

async def send_telegram_document(telegram_id: str, file_path: str, caption: str):
    """Send a document file via Telegram Bot API."""
    bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    
    async with aiohttp.ClientSession() as session:
        with open(file_path, 'rb') as f:
            data = aiohttp.FormData()
            data.add_field('chat_id', str(telegram_id))
            data.add_field('caption', caption)
            data.add_field('document', f, filename=os.path.basename(file_path), content_type='application/pdf')
            
            async with session.post(url, data=data) as response:
                if not response.ok:
                    print(f"❌ Failed to send document to {telegram_id}: {await response.text()}")

@app.post("/okanassist/v1/process-audio")
async def process_audio(telegram_id: str = Form(...), file: UploadFile = File(...)):
    """Process user audio input and route to the correct agent."""
    await initialize_services()
    try:
        if not main_agent:
            raise HTTPException(status_code=503, detail="Service not ready")
        # Step 1: Get user data
        user_data = await get_user_data(AuthCheckRequest(telegram_id=telegram_id))
        supabase_id = user_data.get('user_id', None)
        lang = user_data.get('language', 'en')
        user_timezone = user_data.get('timezone', 'UTC')

        # Save uploaded audio file temporarily
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as temp_file:
            content = await file.read()
            temp_file.write(content)
            ogg_path = temp_file.name
        mp3_path = ogg_path.replace(".ogg", ".mp3")
        mp3_path=await convert_audio(ogg_path, mp3_path)
        # Step 2: Route audio through main agent
        result = await main_agent.route_audio(user_data, mp3_path)
        
        # Check if the result is a dictionary with a 'type' key, and extract content
        if isinstance(result, dict) and result.get("type") == "text":
            final_message = result.get("content")
        else:
            # Fallback for raw string responses or other types
            final_message = str(result)

        # Clean up temp file
        os.unlink(ogg_path)
        os.unlink(mp3_path)

        return {"success": True, "message": final_message}
    except HTTPException:
        raise
    except Exception as e:
        if 'temp_path' in locals():
            try:
                os.unlink(ogg_path)
                os.unlink(mp3_path)
            except:
                pass
        print(f"❌ Unexpected error in process_audio: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

####### Transactions Endpoints


@app.post("/okanassist/v1/process-notification")
async def process_notification(request: NotificationRequest):
    """Handle automated transactions parser - REQUIRES AUTHENTICATION"""
    print("here: in process_notification")
    await initialize_services()
    try:
        # Step 1: Check if telegram_id is provided; if not, fetch it using fetch_telegram_id
        telegram_id = request.telegram_id
        if not telegram_id:
            fetch_result = await fetch_telegram_id(request)
            if not fetch_result.get("success"):
                raise HTTPException(status_code=404, detail="Telegram ID not found for the provided email")
            telegram_id = fetch_result["telegram_id"]
        
        # Step 2: Proceed with authentication using get_user_data
        user_data = await get_user_data(AuthCheckRequest(telegram_id=telegram_id))
        # At this point, user is authenticated, and user_data is available
        
        # Step 3: Print the data received (for debugging/logging)
        print(f"📨 Processed notification data: {request.model_dump()}")
        
        # Step 4: Return the response
        return {"message": "Processed", "telegram_id": telegram_id}
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Unexpected error in process_notification: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/okanassist/v1/process-receipt")
async def process_receipt(telegram_id: str=Form(...), file: UploadFile = File(...)):
    """Process receipt image - REQUIRES AUTHENTICATION + CREDITS"""
    await initialize_services()
    try:
        if not transaction_agent:
            raise HTTPException(status_code=503, detail="Service not ready")
        print("here: in process_receipt")
        # Step 1: Get user data using centralized helper
        user_data = await get_user_data(AuthCheckRequest(telegram_id=telegram_id))
        supabase_id = user_data.get('user_id', None)
        lang_code = user_data.get('language', 'en')
        # Step 2: Consume credits (since auth is now verified)
        credit_result = await check_and_consume_credits(supabase_id, 'receipt_processing', 5, user_data)

        # Step 3: Process the receipt
        # Save uploaded file temporarily
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
            content = await file.read()
            temp_file.write(content)
            temp_path = temp_file.name

        result = await transaction_agent.process_receipt_image(user_data, temp_path)

        # Clean up temp file
        os.unlink(temp_path)
        
        # Add credit info to response if not premium
        if not credit_result.get('is_premium', False):
            credits_remaining = credit_result.get('credits_remaining', 0)
            result += get_message("credit_warning", lang_code, credits_remaining=credits_remaining)
        
        return TransactionResponse(success=True, message=result)
        
    except HTTPException:
        raise
    except Exception as e:
        # Clean up temp file on error
        if 'temp_path' in locals():
            try:
                os.unlink(temp_path)
            except:
                pass
        print(f"❌ Unexpected error in process_receipt: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/okanassist/v1/process-bank-statement")
async def process_bank_statement(telegram_id: str = Form(...), file: UploadFile = File(...)):
    """Process bank statement PDF - REQUIRES AUTHENTICATION"""
    await initialize_services()
    try:
        if not transaction_agent:
            raise HTTPException(status_code=503, detail="Service not ready")
        
        # Step 1: Get user data using centralized helper
        user_data = await get_user_data(AuthCheckRequest(telegram_id=telegram_id))
        supabase_id = user_data.get('user_id', None)
        lang_code = user_data.get('language', 'en')
        # Step 2: Consume credits (since auth is now verified) - Note: 0 credits for bank statement
        credit_result = await check_and_consume_credits(supabase_id, 'bank_statement', 5, user_data)

        # Step 3: Process the bank statement
        # Save uploaded file temporarily
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
            content = await file.read()
            temp_file.write(content)
            temp_path = temp_file.name

        result = await transaction_agent.process_bank_statement(supabase_id, temp_path)

        if not credit_result.get('is_premium', False):
            credits_remaining = credit_result.get('credits_remaining', 0)
            result += get_message("credit_warning", lang_code, credits_remaining=credits_remaining)
        

        # Clean up temp file
        os.unlink(temp_path)
        
        return TransactionResponse(success=True, message=result)
        
    except HTTPException:
        raise
    except Exception as e:
        # Clean up temp file on error
        if 'temp_path' in locals():
            try:
                os.unlink(temp_path)
            except:
                pass
        print(f"❌ Unexpected error in process_bank_statement: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/okanassist/v1/get-transaction-summary")
async def get_transaction_summary(request: SummaryRequest):
    """Get transaction summary - REQUIRES AUTHENTICATION"""
    await initialize_services()
    try:
        if not transaction_agent:
            raise HTTPException(status_code=503, detail="Service not ready")
        
        # Step 1: Get user data using centralized helper
        user_data = await get_user_data(AuthCheckRequest(telegram_id=request.telegram_id))
        #start_date = datetime.now() - timedelta(days=request.days)
        #end_date = datetime.now()
        # Step 2: Process the summary via the agent (no credits needed)
        result = await transaction_agent.get_summary(user_data, request.days)

        print(f"Summary result: {result}")
        return {"success": True, "message": result}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Unexpected error in get_transaction_summary: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


### Reminders Endpoints
@app.post("/okanassist/v1/get-reminders")
async def get_reminders(telegram_id: str, limit: int = 10):
    """Get user reminders - REQUIRES AUTHENTICATION"""
    await initialize_services()
    try:
        if not reminder_agent:
            raise HTTPException(status_code=503, detail="Service not ready")
        
        # Step 1: Get user data using centralized helper
        user_data = await get_user_data(AuthCheckRequest(telegram_id=telegram_id))
        
        # Step 2: Process the reminders (no credits needed)
        result = await reminder_agent.get_reminders(user_data)
        return {"success": True, "message": result.get("content")}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Unexpected error in get_reminders: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

## server function to receive batch of reminders and send notifications
@app.post("/okanassist/v1/batch-notify-reminders")
async def batch_notify_reminders(request: Request):
    """Receive batch of reminders and send notifications."""
    await initialize_services()  # <-- ADD THIS LINE
    try:
        data = await request.json()
        reminders = data.get("reminders", [])
        print(f"Received {len(reminders)} reminders for notification.")
        notified = []
        for reminder_data in reminders:
            reminder_id = reminder_data["reminder_id"]
            telegram_id = reminder_data["telegram_id"]
            title = reminder_data["title"]
            description = reminder_data["description"]
            due_datetime = reminder_data["due_datetime"]
            timezone = reminder_data.get("timezone", "UTC")  # Extract timezone

            # Send notification (pass timezone for conversion)
            await send_telegram_notification(telegram_id, title, description, due_datetime, timezone)

            # Mark as notified in DB
            await supabase_client.database.mark_reminder_notified(reminder_id)
            notified.append(reminder_id)

            # --- Recurring logic ---
            is_recurring = reminder_data.get("is_recurring", False)
            recurrence_pattern = reminder_data.get("recurrence_pattern")
            if is_recurring and recurrence_pattern:
                dt = datetime.fromisoformat(due_datetime.replace("Z", "+00:00"))
                if recurrence_pattern == "daily":
                    next_due = dt + timedelta(days=1)
                elif recurrence_pattern == "weekly":
                    next_due = dt + timedelta(weeks=1)
                elif recurrence_pattern == "monthly":
                    next_due = dt + relativedelta(months=1)
                else:
                    next_due = None

                if next_due:
                    await supabase_client.database.update_reminder_due_datetime(
                        reminder_id,
                        next_due.isoformat().replace("+00:00", "Z")
                    )

        return {"success": True, "notified_count": len(notified), "reminder_ids": notified}
    except Exception as e:
        print(f"❌ Error in batch_notify_reminders: {e}")
        return {"success": False, "error": str(e)}

##### User Management Endpoints
@app.post("/okanassist/v1/register")
async def register_user(request: RegisterRequest):
    """Register new user using Supabase Auth"""
    await initialize_services()
    lang_code = request.language_code
    try:
        if not supabase_client or not timezone_agent:
            raise HTTPException(status_code=503, detail="Service not ready")
        
        # Try to check if the user is already registered/authenticated
        user_data = None
        try:
            user_data = await get_user_data(AuthCheckRequest(telegram_id=request.telegram_id))
            print("User already authenticated:", user_data)
        except HTTPException as e:
            if e.status_code not in (401, 404):
                print(f"❌ Error checking existing user in register_user: {e}")
                raise
            user_data = None

        if user_data:
            return {
                "success": False, 
                "message": get_message("already_registered", lang_code,email=user_data.get('email','unknown'))
            }
        
        # --- Registration process continues here ---
        raw_timezone_input = request.timezone
        processed_timezone, utc_offset = await timezone_agent.identify_timezone(
            language=lang_code, 
            text_input=raw_timezone_input
        )
        print(f"Processed timezone: {processed_timezone}, UTC offset: {utc_offset}")

        if not processed_timezone:
            print(f"⚠️ Timezone identification failed for input '{raw_timezone_input}'. Defaulting to UTC.")
            processed_timezone = "UTC"
        inferred_currency = infer_currency(processed_timezone)
        print(f"Inferred currency: {inferred_currency}")
        
        # Create new user in Supabase Auth
        auth_result = await supabase_client.sign_up_user_with_auth(
            email=request.email,
            password=None,
            user_metadata={
                'name': request.name,
                'telegram_id': request.telegram_id,
                'language_code': request.language_code,
                'registration_source': 'telegram_bot'
            }
        )
        
        if not auth_result['success']:
            return {
                "success": False,
                "message": get_message("registration_failed", lang_code, message=auth_result['message'])
            }
        
        # Create user settings in the database and already link telegram_id
        result = await supabase_client.create_new_user_settings(
            auth_result['user_id'],
            {
                'name': request.name,
                'telegram_id': request.telegram_id,
                'language': request.language_code,
                'timezone': processed_timezone,
                'currency': inferred_currency
            }
        )

        if result.get("success"):
            user_data = {
                'user_id': auth_result['user_id'],
                'email': request.email,
                'name': request.name,
                'currency': inferred_currency,
                'language': request.language_code,
                'timezone': processed_timezone,
                'is_premium': False,
                'premium_until': None,
                'telegram_id': request.telegram_id,
                'authenticated': True
            }
            session_manager.create_session(request.telegram_id, user_data)
            return {
                "success": True,
                "message": get_message("registration_success", lang_code, name=request.name, password=auth_result['password'], download_url=os.getenv("APP_DOWNLOAD_URL", "https://play.google.com/store/apps/details?id=com.okanassist")),
                "user_data": user_data
            }
        else:
            return {
                "success": False,
                "message": get_message("registration_linking_failed", lang_code)
            }
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Registration error API register_user: error aqui {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/okanassist/v1/profile")
async def get_profile(telegram_id: str):
    """Get user profile - REQUIRES AUTHENTICATION"""
    await initialize_services()
    try:
        # Step 1: Get user data using centralized helper
        user_data = await get_user_data(AuthCheckRequest(telegram_id=telegram_id))
        if user_data.get("is_premium"):
            customer_id = await supabase_client.get_customer_id_from_payments(user_data["user_id"])
            manage_url = await supabase_client.create_customer_portal_link(customer_id)
            return {"success": True, "user_data": user_data, "manage_url": manage_url}
        else:
            return {"success": True, "user_data": user_data, "manage_url": None}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Unexpected error in get_profile: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/okanassist/v1/health")
async def health_check():
    """Health check endpoint"""
    await initialize_services()
    return {
        "status": "healthy", 
        "service": "okanassist-ai",
        "timestamp": datetime.now().isoformat(),
        "services": {
            "supabase_client": supabase_client is not None,
            "transaction_agent": transaction_agent is not None,
            "reminder_agent": reminder_agent is not None,
            "main_agent": main_agent is not None
        }
    }

##### HELPER FUNCTIONS #####
# Centralized authentication function
async def check_authentication(request: AuthCheckRequest) -> Dict[str, Any]:
    """
    Centralized user authentication flow.
    Returns user_data on success, raises HTTPException on failure.
    """
    lang_code = request.language
    try:
        # First, check if the telegram_id is linked to a supabase user
        user_data = await supabase_client.get_user_by_telegram_id_auth(request.telegram_id)
        if user_data:
            print(f"🔍 Found linked user for telegram_id {request.telegram_id}")
            return await _validate_and_complete_user_data(user_data, request.telegram_id)

        # If not found, try to link if supabase_user_id is provided
        if request.supabase_user_id:
            print("Trying to link:", request)
            try:
                result = await supabase_client.link_telegram_user(
                    request.supabase_user_id, 
                    request.telegram_id, 
                )
                if result.get("success"):
                    # After successful link, fetch the complete user data again
                    user_data = await supabase_client.get_user_by_telegram_id_auth(request.telegram_id)
                    print("Linking successful, fetched user_data:", user_data)
                    if user_data:
                        return await _validate_and_complete_user_data(user_data, request.telegram_id)
                
                # If linking or re-fetching fails, raise an exception
                raise HTTPException(status_code=401, detail=get_message("link_failed", lang_code))

            except Exception as e:
                print(f"❌ Error linking user in check_authentication: {e}")
                raise HTTPException(status_code=401, detail=get_message("link_failed", lang_code))
        
        # If no user found and no supabase_user_id to link, they must register
        else:
            print(f"⚠️ User {request.telegram_id} not registered.")
            raise HTTPException(status_code=401, detail=get_message("user_not_registered", lang_code))

    except HTTPException:
        raise # Re-raise known HTTP exceptions
    except Exception as e:
        print(f"❌ Uncaught error in check_authentication: {e}")
        raise HTTPException(status_code=500, detail="An error occurred during authentication.")

##used for sending payments telegram messages to users
async def send_telegram_message(telegram_id: str, message: str):
    """Send message via Telegram Bot API"""
    bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    async with aiohttp.ClientSession() as session:
        await session.post(url, json={"chat_id": telegram_id, "text": message})

#function used when the notification system sends the first automated transaction to api
async def fetch_telegram_id(request: NotificationRequest):
    """Fetch Telegram ID by email"""
    await initialize_services()
    try:
        if not supabase_client:
            raise HTTPException(status_code=503, detail="Service not ready")
        telegram_id = await supabase_client.get_telegram_id_by_email(request.email)
        if not telegram_id:
            return {"success": False, "message": "Telegram ID not found"}
        return {"success": True, "telegram_id": telegram_id}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Unexpected error in fetch_telegram_id: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")



async def send_telegram_notification(telegram_id: str, title: str, description: str, due_datetime: str, timezone: str = "UTC"):
    """Send notification via Telegram bot, converting due_datetime to user's timezone."""
    try:
        # Parse UTC datetime
        utc_dt = datetime.fromisoformat(due_datetime.replace("Z", "+00:00")).replace(tzinfo=pytz.utc)
        
        # Convert to user's timezone
        user_tz = pytz.timezone(timezone)
        local_dt = utc_dt.astimezone(user_tz)
        
        # Format for display (e.g., "2025-09-29 10:00")
        formatted_due = local_dt.strftime('%Y-%m-%d %H:%M')
        
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        message = f"🔔 Reminder: {title}\n\n{description}\n\nDue: {formatted_due} ({timezone})"
        send_telegram_message(telegram_id, message)
    #     async with aiohttp.ClientSession() as session:
    #         await session.post(url, json={
    #             "chat_id": telegram_id,
    #             "text": message,
    #             "parse_mode": "Markdown"
    #         })
    except Exception as e:
        print(f"❌ Error sending Telegram notification: {e}")

# Wrap the session manager access and use the exception-based check_authentication
async def get_user_data(auth_request: AuthCheckRequest) -> Dict[str, Any]:
    """
    Helper to get user data from session or database.
    Handles authentication and session creation.
    """
    try:
        # 1. Check for a valid and complete session first
        if session_manager.is_authenticated(auth_request.telegram_id):
            session = session_manager.get_session(auth_request.telegram_id)
            if session and _is_user_data_complete(session):
                print(f"✅ Retrieved complete user data from session for {auth_request.telegram_id}")
                return session
        
        # 2. If no valid session, perform full authentication
        print(f"🔍 No valid session for {auth_request.telegram_id} - performing full authentication.")
        user_data = await check_authentication(auth_request)
        
        # 3. On successful authentication, create a new session
        session_manager.create_session(auth_request.telegram_id, user_data)
        print(f"✅ Session created for {auth_request.telegram_id}")
        
        return user_data

    except HTTPException as e:
        # Log and re-raise HTTP exceptions from check_authentication
        print(f"❌ Authentication failed for {auth_request.telegram_id}: {e.detail}")
        raise
    except Exception as e:
        # Catch any other unexpected errors
        print(f"❌ Unexpected error in get_user_data: {e}")
        raise HTTPException(status_code=500, detail="Internal server error during data retrieval.")


# Helper function to validate and complete user data
async def _validate_and_complete_user_data(user_data: Dict[str, Any], telegram_id: str) -> Dict[str, Any]:
    """Validate user data completeness and fill in missing fields if possible"""
    required_fields = ['user_id', 'email', 'name', 'authenticated']
    
    # Check if all required fields are present and non-empty
    for field in required_fields:
        if not user_data.get(field):
            print(f"⚠️ Missing or empty field '{field}' in user data for {telegram_id} - attempting to complete")
            # Try to fetch from Supabase Auth if user_id is available
            if user_data.get('user_id'):
                try:
                    #auth_user = await supabase_client.supabase.auth.admin.get_user_by_id(user_data['user_id'])
                    auth_user=await supabase_client.get_user_by_telegram_id_auth(telegram_id) # Avoid bugs
                    if auth_user:
                        user_data['email'] = auth_user.get('email') or user_data.get('email', '')
                        user_data['name'] = auth_user.get('name') or user_data.get('name', 'Unknown')
                        #user_data['last_name'] = auth_user.user.user_metadata.get('last_name', user_data.get('last_name', ''))
                        user_data['authenticated'] = True
                        print(f"✅ Completed user data for {telegram_id}")
                except Exception as e:
                    print(f"❌ Failed to complete user data for {telegram_id}: {e}")
            break  # Stop after first missing field to avoid redundant calls
    
    return user_data

# Helper function to check if user data is complete
def _is_user_data_complete(user_data: Dict[str, Any]) -> bool:
    """Check if user data has all required fields"""
    required_fields = ['user_id', 'email', 'name', 'authenticated']
    return all(user_data.get(field) for field in required_fields)


async def check_and_consume_credits(user_id: str, operation_type: str, credits_needed: int, user_data: Dict[str, Any] = None) -> dict:
    """Check and consume credits before processing - Assumes auth is already verified"""
    if not supabase_client:
        raise HTTPException(status_code=503, detail="Service not ready")
    
    # REMOVED: No need to re-check authentication here (it's done upstream)
    # Use the provided user_data or raise an error if not provided
    if not user_data:
        raise HTTPException(status_code=401, detail="User data not provided - authentication required")
    
    # Try to consume credits
    result = await supabase_client.consume_credits(
        user_id, operation_type, credits_needed
    )
    
    if not result['success']:
        if result.get('error') == 'insufficient_credits':
            credits_available = result.get('credits_available', 0)
            credits_needed = result.get('credits_needed', 0)
            
            error_msg = MESSAGES["insufficient_credits"].format(
                credits_available=credits_available,
                credits_needed=credits_needed
            )
            return {
                "success": False,
                "message": error_msg
            }
    
    return result


def infer_currency(timezone: str) -> str:
    """Infer currency code from timezone (basic mapping, can be expanded)."""
    timezone_currency_map = {
        "America/Sao_Paulo": "BRL",
        "America/New_York": "USD",
        "America/Los_Angeles": "USD",
        "Europe/London": "GBP",
        "Europe/Madrid": "EUR",
        "Europe/Paris": "EUR",
        "Europe/Berlin": "EUR",
        "Asia/Tokyo": "JPY",
        "Asia/Shanghai": "CNY",
        "Asia/Kolkata": "INR",
        "Australia/Sydney": "AUD",
        "Africa/Johannesburg": "ZAR",
        "UTC": "USD",
    }
    currency = timezone_currency_map.get(timezone)
    if currency:
        return currency
    if timezone.startswith("America/"):
        return "USD"
    elif timezone.startswith("Europe/"):
        return "EUR"
    elif timezone.startswith("Asia/"):
        return "USD"
    elif timezone.startswith("Australia/"):
        return "AUD"
    elif timezone.startswith("Africa/"):
        return "USD"
    return "USD"

async def convert_audio(input_path: str, output_path: str) -> str:
    """
    Convert .ogg audio file to .mp3 using ffmpeg.
    Returns the output file path.
    """
    try:
        # Run ffmpeg to convert .ogg to .mp3
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, output_path],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        return output_path
    except Exception as e:
        print(f"❌ Audio conversion failed: {e}")
        raise RuntimeError("Audio conversion failed")

