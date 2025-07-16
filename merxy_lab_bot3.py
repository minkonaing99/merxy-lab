from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)
from PIL import Image
import pytesseract
import boto3
import os
import creds
import re
from datetime import datetime, timezone
from boto3.dynamodb.conditions import Key, Attr
import platform
import logging

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

if platform.system() == "Windows":
    pytesseract.pytesseract.tesseract_cmd = r"C:\\Program Files\\Tesseract-OCR\\tesseract.exe"
else:
    pytesseract.pytesseract.tesseract_cmd = "tesseract"

# -------------------- AWS Setup --------------------
dynamodb = boto3.resource(
    'dynamodb',
    aws_access_key_id=creds.AWS_ACCESS_KEY,
    aws_secret_access_key=creds.AWS_SECRET_KEY,
    region_name=creds.REGION_NAME
)
s3 = boto3.client(
    's3',
    aws_access_key_id=creds.AWS_ACCESS_KEY,
    aws_secret_access_key=creds.AWS_SECRET_KEY,
    region_name=creds.REGION_NAME
)

# -------------------- Telegram States --------------------
AWAITING_IMAGE = 1
CHANNEL_ID = creds.CHANNEL_ID
ADMIN_CHANNEL_ID = creds.ADMIN_CHANNEL_ID

# -------------------- DB Helpers --------------------
def log_payment_to_dynamodb(user_id, username, file_name, extracted_data: dict):
    table = dynamodb.Table('merxylab-payment')
    item = {
        "user_id": str(user_id),
        "username": username or "N/A",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "file_name": file_name,
        "transaction_no": extracted_data.get("Transaction No", ""),
        "amount": extracted_data.get("Amount", ""),
        "transaction_time": extracted_data.get("Transaction Time", ""),
        "notes": extracted_data.get("Notes", "")
    }
    table.put_item(Item=item)


def mark_user_as_invited(user_id):
    table = dynamodb.Table('merxylab-invited_users')
    table.put_item(Item={"user_id": str(user_id), "invited": True})

def has_user_been_invited(user_id):
    table = dynamodb.Table('merxylab-invited_users')
    response = table.get_item(Key={"user_id": str(user_id)})
    return response.get("Item", {}).get("invited", False)

def is_duplicate_transaction(transaction_no: str) -> bool:
    table = dynamodb.Table('merxylab-payment')
    try:
        response = table.scan(
            FilterExpression=Attr('transaction_no').eq(transaction_no)
        )
        return response['Count'] > 0
    except Exception as e:
        logger.error(f"[DynamoDB ERROR] Duplicate check failed: {e}")
        return False

def mark_user_as_started(user_id):
    table = dynamodb.Table('merxylab-startedusers')
    table.put_item(Item={
        "user_id": str(user_id),
        "has_started": True,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

def has_user_started(user_id):
    table = dynamodb.Table('merxylab-startedusers')
    response = table.get_item(Key={"user_id": str(user_id)})
    return response.get("Item", {}).get("has_started", False)

def mark_user_as_paid(user, transaction_no):
    table = dynamodb.Table('merxylab-paid_users')
    table.put_item(Item={
        "user_id": str(user.id),
        "name": user.full_name,
        "username": user.username or "N/A",
        "has_paid": True,
        "payment_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "transaction_no": transaction_no
    })

def has_user_paid(user_id):
    table = dynamodb.Table('merxylab-paid_users')
    response = table.get_item(Key={"user_id": str(user_id)})
    return response.get("Item", {}).get("has_paid", False)

# -------------------- Commands --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not has_user_started(user_id):
        mark_user_as_started(user_id)

    await update.message.reply_text(
        "👋 Hello, welcome from Merxy's Lab.\n"
        "This is Merxy's Assistant, who will help you buy the course.\n\n"
        "If you decide to buy, please click /pay."
    )

async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if has_user_paid(user_id):
        await update.message.reply_text(
            "💚 Thank you! Your payment has already been confirmed.\n\n"
            "If you haven't received your access, please contact support."
        )
        return

    await update.message.reply_text(
        "💳 Currently I can only accept KBZPay\n\n"
        "Amount: 5000 Ks\n"
        "Name: Min Ko Naing\n"
        "Phone: 09787753307\n"
        "Notes: Shopping, payment\n\n"
        "If you've completed the transfer, click on /payment_confirm."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 Available Commands:\n"
        "/start - Start chatting with the bot\n"
        "/pay - Payment instructions\n"
        "/payment_confirm - Confirm your payment\n"
        "/help - Show this help message\n"
        "/end - End the session"
    )

async def end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Bot session ended. You can /start again anytime.")
    return ConversationHandler.END

async def start_payment_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if has_user_paid(user_id):
        await update.message.reply_text(
            "💚 Thank you! Your payment has already been confirmed.\n\n"
            "If you haven't received your access or need help, please contact support."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "📸 Please send your KBZPay payment screenshot from History section.\n\n"
        "⚠️ Important:\n"
        "1. Make sure the screenshot shows complete transaction details\n"
        "2. Send the original image (not cropped or edited)\n"
        "3. The image should be clear and readable\n\n"
        "You have 2 minutes to send the image or this session will timeout."
    )
    return AWAITING_IMAGE

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Payment confirmation cancelled.")
    return ConversationHandler.END

# -------------------- OCR Logic --------------------
def is_valid_kpay_text(text: str) -> bool:
    keywords = ["Transaction Time", "Transaction No", "Transfer To", "Amount", "Notes"]
    return all(kw.lower() in text.lower() for kw in keywords)

def extract_payment_info(text: str) -> tuple[str, dict]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    result = {
        "Transaction Time": "Not Found",
        "Transaction No": "Not Found",
        "Transaction Type": "Not Found",
        "Transfer To": "Not Found",
        "Amount": "Not Found",
        "Notes": "Not Found",
    }
    for idx, line in enumerate(lines):
        if result["Transaction Time"] == "Not Found" and re.search(r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}", line):
            result["Transaction Time"] = line
        if result["Transaction No"] == "Not Found" and re.fullmatch(r"\d{17,20}", line):
            result["Transaction No"] = line
        if result["Amount"] == "Not Found" and re.search(r"-?\d{1,3}(,\d{3})+.*Ks", line, re.IGNORECASE):
            result["Amount"] = line
        if result["Transaction Type"] == "Not Found" and re.search(r"(Transfer|Top[- ]?up|Payment Successful|Receive)", line, re.IGNORECASE):
            result["Transaction Type"] = line
    for idx in range(len(lines) - 1):
        if re.match(r"^[A-Za-z .]{3,}$", lines[idx]) and re.search(r"[\(*#\d+]{5,}", lines[idx + 1]):
            result["Transfer To"] = f"{lines[idx]}\n{lines[idx + 1]}"
            break
    for idx in reversed(range(len(lines))):
        if lines[idx] not in result.values():
            result["Notes"] = lines[idx]
            break
    summary = "\n".join([f"*{key}:* `{value}`" for key, value in result.items()])
    return summary, result
def extract_text_from_image(image_path):
    image = Image.open(image_path)
    
    # Try English first
    text = pytesseract.image_to_string(image, lang='eng')
    if not re.search(r'[a-zA-Z]', text):  # If no English text detected
        text = pytesseract.image_to_string(image, lang='mya')
    
    return text


def extract_fields(text):
    result = {
        "time": None,
        "transaction_id": None,
        "amount": None,
        "name": None,
        "notes": None
    }

    text = re.sub(r'\s+', ' ', text).strip()

    # English format
    eng_time = re.search(r'Transaction Time\s*([\d/]+ [\d:]+)', text)
    eng_id = re.search(r'Transaction No\.?\s*(\d{16,20})', text)
    eng_amount = re.search(r'Amount\s*(-?\d[\d,]*\.?\d*)\s*Ks', text)

    if eng_time or eng_id or eng_amount:
        if eng_time:
            result["time"] = eng_time.group(1)
        if eng_id:
            result["transaction_id"] = eng_id.group(1)
        if eng_amount:
            amount = eng_amount.group(1).replace(',', '')
            result["amount"] = f"{amount} Ks"

        name_match = re.search(r'Transfer To\s*([A-Z][A-Za-z\s]+)\s*[\(<]?[*#]+(\d{4})[\)>]?', text)
        if not name_match:
            name_match = re.search(r'Transfer To\s*([A-Z][A-Za-z\s]+)\s*[*#]+\d{4}', text)
        if name_match:
            result["name"] = f"{name_match.group(1).strip()} ({name_match.group(2) if len(name_match.groups()) > 1 else name_match.group(1).split()[-1]})"

        notes_match = re.search(r'Notes\s*([^\n]+?)(?=\s*(?:Transaction|Transfer|Amount|$))', text)
        if notes_match:
            result["notes"] = notes_match.group(1).strip()
        else:
            amount_pos = text.find('Amount') if 'Amount' in text else -1
            if amount_pos > -1:
                notes_part = text[amount_pos:].split('Ks')[-1].strip()
                if notes_part and not any(x in notes_part for x in ['Transaction', 'Transfer']):
                    result["notes"] = notes_part.split('\n')[0].strip()

        return result

    # Myanmar fallback
    my_time = re.search(r'(\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})', text)
    my_id = re.search(r'(\d{16,20})', text)
    my_amount = re.search(r'(-?\d[\d,]*\.?\d*)\s*Ks', text)

    if my_time or my_id or my_amount:
        if my_time:
            result["time"] = my_time.group(1)
        if my_id:
            result["transaction_id"] = my_id.group(1)
        if my_amount:
            amount = my_amount.group(1).replace(',', '')
            result["amount"] = f"{amount} Ks"

        name_match = re.search(r'([A-Z][A-Za-z\s]+)\s*[\(<]?[*#]+(\d{4})[\)>]?', text)
        if not name_match:
            name_match = re.search(r'([A-Z][A-Za-z\s]+)\s*[*#]+\d{4}', text)
        if name_match:
            result["name"] = f"{name_match.group(1).strip()} ({name_match.group(2) if len(name_match.groups()) > 1 else name_match.group(1).split()[-1]})"

        amount_pos = text.find('Ks') if 'Ks' in text else -1
        if amount_pos > -1:
            notes_part = text[amount_pos+2:].strip()
            if notes_part and not any(x in notes_part for x in ['Transaction', 'Transfer']):
                result["notes"] = notes_part.split('\n')[0].strip()

        return result

    # Fallback
    time_match = re.search(r'(\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})', text)
    id_match = re.search(r'(\d{16,20})', text)
    amount_match = re.search(r'(-?\d[\d,]*\.?\d*)\s*Ks', text)

    if time_match:
        result["time"] = time_match.group(1)
    if id_match:
        result["transaction_id"] = id_match.group(1)
    if amount_match:
        amount = amount_match.group(1).replace(',', '')
        result["amount"] = f"{amount} Ks"

    name_match = re.search(r'([A-Z][A-Za-z\s]+)\s*[\(<]?[*#]+(\d{4})[\)>]?', text)
    if not name_match:
        name_match = re.search(r'([A-Z][A-Za-z\s]+)\s*[*#]+\d{4}', text)
    if name_match:
        result["name"] = f"{name_match.group(1).strip()} ({name_match.group(2) if len(name_match.groups()) > 1 else name_match.group(1).split()[-1]})"

    return result

# -------------------- Image Handler --------------------
async def handle_payment_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    photo_file = await update.message.photo[-1].get_file()
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{user_id}_{now_str}.png"

    await photo_file.download_to_drive(filename)

    try:
        # OCR and extraction using new logic
        extracted_text = extract_text_from_image(filename)
        extracted_fields = extract_fields(extracted_text)

        # Ensure required fields exist
        if not extracted_fields["transaction_id"] or not extracted_fields["amount"]:
            await update.message.reply_text(
                "⚠️ Couldn't extract valid payment details. Please make sure:\n\n"
                "1. You're sending a screenshot from KBZPay History\n"
                "2. All transaction details are visible\n"
                "3. The image is clear and not blurry\n\n"
                "Please try again with /payment_confirm"
            )
            await context.bot.send_message(
                chat_id=ADMIN_CHANNEL_ID,
                text=(
                    f"⚠️ *OCR Failure Detected*\n"
                    f"👤 *User ID:* `{user_id}`\n"
                    f"🖼️ *File:* `{filename}`\n"
                    "❌ Couldn't extract required fields."
                ),
                parse_mode="Markdown"
            )
            return ConversationHandler.END

        # ✅ Validate name and last 4 digits
        expected_name = "U MIN KO NAING"
        expected_last4 = "3307"
        name_field = extracted_fields["name"] or ""
        if expected_name not in name_field or expected_last4 not in name_field:
            await update.message.reply_text(
                "⚠️ Payment must be made from the registered KBZPay account:\n\n"
                f"{expected_name} ({expected_last4})\n\n"
                "Please double-check and try again /payment_confirm."
            )
            return ConversationHandler.END

        # ✅ Validate amount > 5000 Ks
        amount_str = extracted_fields["amount"].replace("Ks", "").replace(",", "").strip()

# Remove negative sign if present (e.g., "-700000 Ks" becomes "700000")
        if amount_str.startswith('-'):
            amount_str = amount_str[1:]

        try:
            amount_value = float(amount_str)
            if amount_value <= 5000:
                await update.message.reply_text(
                    "⚠️ Payment amount must be more than 5000 Ks.\n"
                    f"Your amount: {amount_value:.0f} Ks"
                    "Try again with the right screenshot by clicking /payment_confirm"
                )
                return ConversationHandler.END
        except ValueError:
            await update.message.reply_text("⚠️ Could not interpret the amount properly.")
            return ConversationHandler.END

        transaction_no = extracted_fields["transaction_id"]

        # ✅ Check for duplicate transaction
        if is_duplicate_transaction(transaction_no):
            await update.message.reply_text(
                "⚠️ This transaction has already been used.\n\n"
                "If you believe this is an error, please contact support."
            )
            return ConversationHandler.END

        # ✅ Upload image to S3
        s3.upload_file(filename, creds.BUCKET_NAME, f"payments/{filename}")

        # ✅ Save to DynamoDB
        log_payment_to_dynamodb(user_id, user.username, filename, {
            "Transaction No": transaction_no,
            "Amount": extracted_fields["amount"],
            "Transaction Time": extracted_fields["time"],
            "Notes": extracted_fields["notes"]
        })

        mark_user_as_paid(user, transaction_no)

        # ✅ Build reply summary
        summary = (
            f"*Transaction No:* `{transaction_no}`\n"
            f"*Amount:* `{extracted_fields['amount']}`\n"
            f"*Time:* `{extracted_fields['time']}`\n"
            f"*Notes:* `{extracted_fields['notes'] or 'N/A'}`"
        )

        await update.message.reply_text("✅ Payment successfully verified!")
        await update.message.reply_text(f"📟 *Payment Details:*\n{summary}", parse_mode="Markdown")

        # ✅ Send invite link if not already sent
        if not has_user_been_invited(user_id):
            try:
                invite_link = await context.bot.create_chat_invite_link(
                    chat_id=CHANNEL_ID,
                    member_limit=1
                )
                await update.message.reply_text(
                    f"📩 Here is your exclusive access link (valid for 24 hours):\n"
                    f"{invite_link.invite_link}\n\n"
                    "⚠️ This link can only be used once. Don't share it with others."
                )
                mark_user_as_invited(user_id)
            except Exception as e:
                logger.error(f"Failed to create invite link: {e}")
                await update.message.reply_text(
                    "✅ Payment verified but failed to generate access link.\n\n"
                    "Please contact support with your transaction number."
                )

        # ✅ Notify admin
        await context.bot.send_message(
            chat_id=ADMIN_CHANNEL_ID,
            text=(
                f" *New Payment Confirmed!*\n\n"
                f" *User:* `{user.full_name}` (`{user_id}`)\n"
                f" *File:* `{filename}`\n"
                f" *Amount:* `{extracted_fields['amount']}`\n"
                f" *Time:* `{extracted_fields['time']}`\n"
                f" *Transaction No:* `{transaction_no}`\n"
                f" *Notes:* `{extracted_fields['notes'] or 'N/A'}`\n"
                f" *Invite Sent:* `{has_user_been_invited(user_id)}`"
            ),
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"[ERROR] {e}")
        await update.message.reply_text("An error occurred while processing the image.")
        await context.bot.send_message(
            chat_id=ADMIN_CHANNEL_ID,
            text=(
                f"🚨 *Payment Error*\n\n"
                f"👤 *User ID:* `{user_id}`\n"
                f"❌ *Error:* `{str(e)}`"
            ),
            parse_mode="Markdown"
        )

    finally:
        if os.path.exists(filename):
            os.remove(filename)

    return ConversationHandler.END

# -------------------- Bot Entry --------------------
if __name__ == '__main__':
    app = ApplicationBuilder().token(creds.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("end", end))

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("payment_confirm", start_payment_confirm)],
        states={AWAITING_IMAGE: [MessageHandler(filters.PHOTO, handle_payment_image)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=120
    )

    app.add_handler(conv_handler)
    logger.info("💬 merxylab_bot is running...")
    app.run_polling()
