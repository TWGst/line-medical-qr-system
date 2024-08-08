import os
from dotenv import load_dotenv
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (MessageEvent, TextMessage, TextSendMessage, 
                            QuickReplyButton, QuickReply, ImageSendMessage)
from flask_cors import CORS
import qrcode
import io
import base64
from datetime import datetime
import re

app = Flask(__name__)
CORS(app)
load_dotenv()

line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))

user_states = {}
user_data = {}

def generate_qr_code(data):
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    img_byte_arr = img_byte_arr.getvalue()
    return base64.b64encode(img_byte_arr).decode()

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text

    if text == "診察券":
        user_states[user_id] = "waiting_card_number"
        message = TextSendMessage(text="診察券の登録を開始します。診察券番号を入力してください。")
    
    elif user_id in user_states:
        if user_states[user_id] == "waiting_card_number":
            user_data[user_id] = {"card_number": text}
            user_states[user_id] = "waiting_name"
            message = TextSendMessage(text="お名前（漢字）を入力してください。")
        
        elif user_states[user_id] == "waiting_name":
            user_data[user_id]["name"] = text
            user_states[user_id] = "waiting_name_kana"
            message = TextSendMessage(text="お名前（カタカナ）を入力してください。")
        
        elif user_states[user_id] == "waiting_name_kana":
            user_data[user_id]["name_kana"] = text
            user_states[user_id] = "waiting_birthdate"
            message = TextSendMessage(text="生年月日を「YYYY-MM-DD」の形式で入力してください。")
        
        elif user_states[user_id] == "waiting_birthdate":
            try:
                datetime.strptime(text, '%Y-%m-%d')
                user_data[user_id]["birthdate"] = text
                user_states[user_id] = "waiting_gender"
                message = TextSendMessage(
                    text="性別を選択してください。",
                    quick_reply=QuickReply(items=[
                        QuickReplyButton(action={"type": "message", "label": "男性", "text": "男性"}),
                        QuickReplyButton(action={"type": "message", "label": "女性", "text": "女性"})
                    ])
                )
            except ValueError:
                message = TextSendMessage(text="無効な日付形式です。YYYY-MM-DDの形式で入力してください。")
        
        elif user_states[user_id] == "waiting_gender":
            if text in ["男性", "女性"]:
                user_data[user_id]["gender"] = text
                user_states[user_id] = "waiting_postal_code"
                message = TextSendMessage(text="郵便番号を入力してください（例：123-4567）。")
            else:
                message = TextSendMessage(text="無効な性別です。「男性」または「女性」を選択してください。")
        
        elif user_states[user_id] == "waiting_postal_code":
            if re.match(r'^\d{3}-\d{4}$', text):
                user_data[user_id]["postal_code"] = text
                user_states[user_id] = "waiting_phone"
                message = TextSendMessage(text="電話番号を入力してください。")
            else:
                message = TextSendMessage(text="無効な郵便番号形式です。正しい形式（例：123-4567）で入力してください。")
        
        elif user_states[user_id] == "waiting_phone":
            if re.match(r'^\d{10,11}$', text.replace('-', '')):
                user_data[user_id]["phone"] = text
                user_states[user_id] = "waiting_email"
                message = TextSendMessage(text="メールアドレスを入力してください（任意：スキップする場合は「スキップ」と入力）。")
            else:
                message = TextSendMessage(text="無効な電話番号形式です。正しい形式で入力してください。")
        
        elif user_states[user_id] == "waiting_email":
            if text.lower() == "スキップ":
                user_data[user_id]["email"] = ""
            elif re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', text):
                user_data[user_id]["email"] = text
            else:
                message = TextSendMessage(text="無効なメールアドレス形式です。正しい形式で入力するか、「スキップ」と入力してください。")
                line_bot_api.reply_message(event.reply_token, message)
                return

            del user_states[user_id]
            qr_data = f"Card Number: {user_data[user_id]['card_number']}, Name: {user_data[user_id]['name']}, Name (Kana): {user_data[user_id]['name_kana']}, Birthdate: {user_data[user_id]['birthdate']}, Gender: {user_data[user_id]['gender']}, Postal Code: {user_data[user_id]['postal_code']}, Phone: {user_data[user_id]['phone']}, Email: {user_data[user_id]['email']}"
            qr_image = generate_qr_code(qr_data)
            message = [
                TextSendMessage(text="診察券の登録が完了しました。以下のQRコードを保存してください。"),
                ImageSendMessage(
                    original_content_url=f"data:image/png;base64,{qr_image}",
                    preview_image_url=f"data:image/png;base64,{qr_image}"
                )
            ]
    
    else:
        message = TextSendMessage(text="「診察券」と入力して、診察券の登録を開始してください。")

    line_bot_api.reply_message(event.reply_token, message)

if __name__ == "__main__":
    app.run(debug=True)