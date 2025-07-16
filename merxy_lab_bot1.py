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
from datetime import datetime
from boto3.dynamodb.conditions import Key, Attr
import platform

if platform.system() == "Windows":
    pytesseract.pytesseract.tesseract_cmd = r"C:\\Program Files\\Tesseract-OCR\\tesseract.exe"
else:
    # On Linux, tesseract is usually in PATH
    pytesseract.pytesseract.tesseract_cmd = "tesseract"


# -------------------- AWS DynamoDB Setup --------------------
dynamodb = boto3.resource(
    'dynamodb',
    aws_access_key_id=creds.AWS_ACCESS_KEY,
    aws_secret_access_key=creds.AWS_SECRET_KEY,
    region_name=creds.REGION_NAME
)

def log_payment_to_dynamodb(user_id, file_name, extracted_data: dict):
    table = dynamodb.Table('merxylab-payment')
    item = {
        "user_id": str(user_id),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "file_name": file_name,
        **extracted_data
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
    """Uses scan() to check for duplicates (no GSI required)."""
    table = dynamodb.Table('merxylab-payment')
    try:
        response = table.scan(
            FilterExpression=Attr('Transaction No').eq(transaction_no)
        )
        return response['Count'] > 0
    except Exception as e:
        print(f"[DynamoDB ERROR] Duplicate check failed: {e}")
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

def mark_user_as_paid(user_id, transaction_no):
    table = dynamodb.Table('merxylab-paid_users')
    table.put_item(Item={
        "user_id": str(user_id),
        "has_paid": True,
        "payment_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "transaction_no": transaction_no
    })

def has_user_paid(user_id):
    table = dynamodb.Table('merxylab-paid_users')
    response = table.get_item(Key={"user_id": str(user_id)})
    return response.get("Item", {}).get("has_paid", False)

# -------------------- AWS S3 Setup --------------------
s3 = boto3.client(
    's3',
    aws_access_key_id=creds.AWS_ACCESS_KEY,
    aws_secret_access_key=creds.AWS_SECRET_KEY,
    region_name=creds.REGION_NAME
)



# -------------------- Telegram States --------------------
AWAITING_IMAGE = 1
CHANNEL_ID = creds.CHANNEL_ID

# -------------------- Command Handlers --------------------
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
        await update.message.reply_text("✅ You've already completed the payment. Thank you!")
        return

    message = (
        "💳 Currently I can only accept KBZPay\n\n"
        "Amount: 5000 Ks\n"
        "Name: Min Ko Naing\n"
        "Phone: 09787753307\n"
        "Notes: Shopping, payment\n\n"
        "If you've completed the transfer, click on /payment_confirm."
    )
    await update.message.reply_text(message)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "📌 *Available Commands:*\n"
        "/start \\- Start chatting with the bot\n"
        "/pay \\- Payment instructions\n"
        "/payment\\_confirm \\- Confirm your payment\n"
        "/help \\- Show this help message\n"
        "/end \\- End the session\n\n"
        "If you have any customer issues, you can connect with Merxy\\."
    )
    await update.message.reply_text(message, parse_mode="MarkdownV2")

async def end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Bot session ended. You can /start again anytime.")
    return ConversationHandler.END

async def start_payment_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 Please send your KBZPay payment screenshot from History in 1 min.")
    return AWAITING_IMAGE

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Payment confirmation cancelled.")
    return ConversationHandler.END

# -------------------- OCR & Extraction --------------------
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
    used_indexes = set()
    skip_labels = {"Details", "Transaction Time", "Transaction No", "Transaction Type", "Transfer To", "Amount", "Notes"}

    for idx, line in enumerate(lines):
        if line in skip_labels:
            continue
        if result["Transaction Time"] == "Not Found" and re.search(r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}", line):
            result["Transaction Time"] = line
            used_indexes.add(idx)
            continue
        if result["Transaction No"] == "Not Found" and re.fullmatch(r"\d{17,20}", line):
            result["Transaction No"] = line
            used_indexes.add(idx)
            continue
        if result["Amount"] == "Not Found" and re.search(r"-?\d{1,3}(,\d{3})+.*Ks", line, re.IGNORECASE):
            result["Amount"] = line
            used_indexes.add(idx)
            continue
        if result["Transaction Type"] == "Not Found" and re.search(r"(Transfer|Top[- ]?up|Payment Successful|Receive)", line, re.IGNORECASE):
            result["Transaction Type"] = line
            used_indexes.add(idx)
            continue

    for idx in range(len(lines) - 1):
        if idx in used_indexes or lines[idx] in skip_labels:
            continue
        name_line = lines[idx]
        masked_line = lines[idx + 1] if idx + 1 < len(lines) else ""
        if (
            re.match(r"^[A-Za-z .]{3,}$", name_line) and
            re.search(r"[\(*#\d+]{5,}", masked_line) and
            "Transaction" not in name_line
        ):
            result["Transfer To"] = f"{name_line}\n{masked_line}"
            used_indexes.update({idx, idx + 1})
            break

    for idx in reversed(range(len(lines))):
        if idx not in used_indexes and lines[idx] not in skip_labels:
            result["Notes"] = lines[idx]
            break

    summary = "📟 *Payment Details:*\n"
    for key, value in result.items():
        summary += f"*{key}:* `{value}`\n"
    return summary, result

# -------------------- Image Handler --------------------
async def handle_payment_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo_file = await update.message.photo[-1].get_file()

    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{user_id}_{now_str}.png"
    local_path = filename

    await photo_file.download_to_drive(local_path)

    try:
        image = Image.open(local_path)
        extracted_text = pytesseract.image_to_string(image)
        print(f"[OCR OUTPUT from {user_id}]\n{extracted_text}")

        if is_valid_kpay_text(extracted_text):
            summary, extracted_dict = extract_payment_info(extracted_text)
            transaction_no = extracted_dict.get("Transaction No", "")

            if is_duplicate_transaction(transaction_no):
                await update.message.reply_text("⚠️ This transaction has already been used. Please send a new one.")
                return ConversationHandler.END

            s3.upload_file(local_path, creds.BUCKET_NAME, f"payments/{filename}")
            log_payment_to_dynamodb(user_id, filename, extracted_dict)
            mark_user_as_paid(user_id, transaction_no)
            await update.message.reply_text("✅ Payment successfully!")
            await update.message.reply_text(summary, parse_mode="Markdown")

            if has_user_been_invited(user_id):
                await update.message.reply_text("ℹ️ You've already received your invite link.")
            else:
                try:
                    invite_link = await context.bot.create_chat_invite_link(
                        chat_id=CHANNEL_ID,
                        member_limit=1
                    )
                    await update.message.reply_text(
                        f"📩 Here is your single-use invite link:\n{invite_link.invite_link}"
                    )
                    mark_user_as_invited(user_id)
                except Exception as e:
                    await update.message.reply_text(
                        f"⚠️ Payment saved, but failed to generate invite link:\n{e}"
                    )
        else:
            await update.message.reply_text("⚠️ Couldn't extract details. Please restart and resend a clearer screenshot from the History.")

    except Exception as e:
        await update.message.reply_text("An error occurred while processing the image.")
        print(f"[ERROR] {e}")
    finally:
        if os.path.exists(local_path):
            os.remove(local_path)

    return ConversationHandler.END

# -------------------- Bot Entry --------------------
if __name__ == '__main__':
    app = ApplicationBuilder().token(creds.bot_token).build()

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
    print("💬 merxylab_bot is running...")
    app.run_polling()
