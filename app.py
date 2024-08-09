import os
import re
import time
import threading
from datetime import datetime
from flask import Flask, request, abort, current_app, send_from_directory
from flask import copy_current_request_context
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import (MessageEvent, TextMessage, TextSendMessage, 
                            QuickReplyButton, QuickReply, ImageSendMessage)
from flask_cors import CORS
import qrcode
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

app = Flask(__name__)
CORS(app)
    
# 環境変数の読み込み
from dotenv import load_dotenv
load_dotenv()

# LINE Bot API の設定
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))

# アプリケーションの設定
app.config['UPLOAD_FOLDER'] = 'static/qr_codes'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# グローバル変数
user_states = {}
user_data = {}

def generate_qr_code(data, user_id):
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    filename = f"qr_{user_id}_{int(time.time())}.png"
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(filename))
    img.save(filepath)
    
    return filename
    
def send_qr_code(user_id, qr_data):
    @copy_current_request_context
    def _send_qr_code(user_id, qr_data):
        try:
            filename = generate_qr_code(qr_data, user_id)
            if filename:
                host = request.host_url.rstrip('/')
                qr_url = f"{host}/static/qr_codes/{filename}"
                message = [
                    TextSendMessage(text="診察券の登録が完了しました。以下のQRコードを保存してください。"),
                    ImageSendMessage(
                        original_content_url=qr_url,
                        preview_image_url=qr_url
                    )
                ]
                line_bot_api.push_message(user_id, message)
                current_app.logger.info(f"QRコードを送信しました: {user_id}")
            else:
                line_bot_api.push_message(user_id, TextSendMessage(text="QRコードの生成に失敗しました。"))
        except LineBotApiError as e:
            current_app.logger.error(f"LINE API エラー: {str(e)}")
        except Exception as e:
            current_app.logger.error(f"QRコード送信エラー: {str(e)}")

    threading.Thread(target=_send_qr_code, args=(user_id, qr_data)).start()

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

            qr_data = f"Card Number: {user_data[user_id]['card_number']}, Name: {user_data[user_id]['name']}, Name (Kana): {user_data[user_id]['name_kana']}, Birthdate: {user_data[user_id]['birthdate']}, Gender: {user_data[user_id]['gender']}, Postal Code: {user_data[user_id]['postal_code']}, Phone: {user_data[user_id]['phone']}, Email: {user_data[user_id]['email']}"
            send_qr_code(user_id, qr_data)
            
            message = TextSendMessage(text="診察券の登録が完了しました。まもなくQRコードが送信されます。")
            del user_states[user_id]
    
    else:
        message = TextSendMessage(text="「診察券」と入力して、診察券の登録を開始してください。")

    line_bot_api.reply_message(event.reply_token, message)

@app.route('/')
def index():
    return "LINE診察券システムが正常に動作しています。"

@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('static', path)


if __name__ == "__main__":
    app.run(debug=True, ssl_context='adhoc')