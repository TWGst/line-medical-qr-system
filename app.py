import os
import re
import time
import threading
import logging
import sys
import io
from datetime import datetime
from flask import Flask, render_template, jsonify, request, abort, current_app, send_from_directory, url_for
from flask import copy_current_request_context
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import (MessageEvent, TextMessage, TextSendMessage, QuickReplyButton, QuickReply, ImageSendMessage)
from flask_cors import CORS
import qrcode
from werkzeug.utils import secure_filename
from werkzeug.middleware.shared_data import SharedDataMiddleware
import gspread
from oauth2client.service_account import ServiceAccountCredentials

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# ログ設定
logging.basicConfig(level=logging.DEBUG)

# 環境変数の読み込み
from dotenv import load_dotenv
load_dotenv()

# Flaskアプリケーションの初期化
app = Flask(__name__, static_url_path='/static', static_folder='static')
CORS(app)

# アプリケーションの設定
app.config['UPLOAD_FOLDER'] = os.path.join(app.static_folder, 'qr_codes')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
app.wsgi_app = SharedDataMiddleware(app.wsgi_app, {
    '/static': app.static_folder
})

# 静的ファイルのディレクトリを確認
static_qr_folder = os.path.join(app.static_folder, 'qr_codes')
os.makedirs(static_qr_folder, exist_ok=True)
app.logger.info(f"Static QR folder: {static_qr_folder}")

# ログ情報の追加
app.logger.info(f"Current working directory: {os.getcwd()}")
app.logger.info(f"UPLOAD_FOLDER path: {app.config['UPLOAD_FOLDER']}")

# スプレッドシートのJSONキー
credentials_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')

# Google Sheets APIの設定
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_name(credentials_path, scope)
client = gspread.authorize(creds)

# スプレッドシートを開く
sheet = client.open(os.getenv('SPREADSHEET_NAME')).sheet1
# UTF-8エンコーディングを設定
sheet.client.session.encoding = 'utf-8'

# スプレッドシートの列見出しを更新
headers = ['Timestamp', 'Card Number', 'Name', 'Name (Kana)', 'Birthdate', 'Gender', 'Postal Code', 'Phone', 'Email']
try:
    if sheet.row_values(1) != headers:
        sheet.insert_row(headers, 1)
        app.logger.info("Spreadsheet headers updated")
except Exception as e:
    app.logger.error(f"Failed to update spreadsheet headers: {str(e)}")

# LINE Bot API の設定
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))

# グローバル変数
user_states = {}
user_data = {}

@app.before_request
def log_request_info():
    app.logger.debug('Headers: %s', request.headers)
    app.logger.debug('Body: %s', request.get_data())

@app.after_request
def log_response_info(response):
    app.logger.debug('Response Status: %s', response.status)
    if response.is_sequence:
        app.logger.debug('Response: %s', response.get_data())
    else:
        app.logger.debug('Response: [Binary data]')
    return response

def generate_qr_code(data, user_id):
    try:
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(data.decode('utf-8') if isinstance(data, bytes) else data)  # バイト列の場合はデコード
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        
        filename = f"qr_{user_id}_{int(time.time())}.png"
        filepath = os.path.join(static_qr_folder, filename)
        img.save(filepath)

        app.logger.info(f"QR code generated and saved: {filepath}")
        app.logger.info(f"File exists: {os.path.exists(filepath)}")
        return filename
    except Exception as e:
        app.logger.error(f"Error generating QR code: {str(e)}", exc_info=True)
        return None

def send_qr_code(user_id, qr_data):
    @copy_current_request_context
    def _send_qr_code(user_id, qr_data):
        try:
            filename = generate_qr_code(qr_data, user_id)
            if filename:
                qr_url = f"{request.url_root.rstrip('/')}/static/qr_codes/{filename}?ngrok-skip-browser-warning=true"
                app.logger.info(f"Generated QR code URL: {qr_url}")
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
            current_app.logger.error(f"QRコード送信エラー: {str(e)}", exc_info=True)

    threading.Thread(target=_send_qr_code, args=(user_id, qr_data)).start()

def update_spreadsheet(user_data):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')  # タイムスタンプを生成
    row = [
        timestamp,  # タイムスタンプを最初の列に追加
        user_data.get('Card Number', ''),
        user_data.get('Name', ''),
        user_data.get('Name (Kana)', ''),
        user_data.get('Birthdate', ''),
        user_data.get('Gender', ''),
        user_data.get('Postal Code', ''),
        user_data.get('Phone', ''),
        user_data.get('Email', '')
    ]
    # Unicode文字列に変換
    row = [str(item) for item in row]
    sheet.append_row(row)
    app.logger.info(f"Data added to spreadsheet: {row}")  # ログ追加

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.error("Invalid signature")
        return 'OK', 200  # LINEプラットフォームには常に200を返す
    except Exception as e:
        app.logger.error(f"Unexpected error: {str(e)}")
        return 'OK', 200  # エラーが発生しても200を返す
    return 'OK', 200

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text
    app.logger.info(f"Received message from {user_id}: {text}")

    message = None  # Initialize message variable

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
            message = TextSendMessage(text="生年月日を「YYYY年MM月DD日」の形式で入力してください。（例：1990年01月01日）")
        
        elif user_states[user_id] == "waiting_birthdate":
            try:
                # YYYY年MM月DD日 形式をパースする
                date_parts = re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日', text)
                if date_parts:
                    year, month, day = map(int, date_parts.groups())
                    birthdate = datetime(year, month, day)
                    user_data[user_id]["birthdate"] = birthdate.strftime('%Y年%m月%d日')  # 保存形式を統一
                    user_states[user_id] = "waiting_gender"
                    message = TextSendMessage(
                        text="性別を選択してください。",
                        quick_reply=QuickReply(items=[
                            QuickReplyButton(action={"type": "message", "label": "男性", "text": "男性"}),
                            QuickReplyButton(action={"type": "message", "label": "女性", "text": "女性"})
                        ])
                    )
                else:
                    raise ValueError("Invalid date format")
            except ValueError:
                message = TextSendMessage(text="無効な日付形式です。YYYY年MM月DD日の形式で入力してください。（例：1990年01月01日）")  

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

            qr_data = (f"Card Number: {user_data[user_id]['card_number']}, "
                        f"Name: {user_data[user_id]['name']}, "
                        f"Name (Kana): {user_data[user_id]['name_kana']}, "
                        f"Birthdate: {user_data[user_id]['birthdate']}, "
                        f"Gender: {user_data[user_id]['gender']}, "
                        f"Postal Code: {user_data[user_id]['postal_code']}, "
                        f"Phone: {user_data[user_id]['phone']}, "
                        f"Email: {user_data[user_id]['email']}")
            qr_data = qr_data.encode('utf-8')  # UTF-8エンコーディングを確保
            send_qr_code(user_id, qr_data)
            
            message = TextSendMessage(text="診察券の登録が完了しました。まもなくQRコードが送信されます。")
            del user_states[user_id]
            
    else:
        message = TextSendMessage(text="「診察券」と入力して、診察券の登録を開始してください。")

    if message:
        line_bot_api.reply_message(event.reply_token, message)
        app.logger.info(f"Sent reply to {user_id}: {message}")
    else:
        app.logger.warning(f"No message to send for user {user_id}")

@app.route('/camera')
def camera():
    return render_template('camera.html')

@app.route('/scan_qr', methods=['POST'])
def scan_qr():
    data = request.json
    qr_data = data.get('qr_data')
    
    if not qr_data:
        return jsonify({"error": "QR data is missing"}), 400
    
    # QRコードデータの解析
    info = {}
    for item in qr_data.split(", "):
        key, value = item.split(": ", 1)
        info[key] = value.encode('iso-8859-1').decode('utf-8')  # エンコーディングの修正
    
    # スプレッドシートに登録
    try:
        update_spreadsheet(info)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        app.logger.info(f"QR code scanned and data registered at {timestamp}")
        return jsonify({"message": f"データが正常に登録されました。スキャン時刻: {timestamp}"}), 200
    except Exception as e:
        app.logger.error(f"Failed to update spreadsheet: {str(e)}")
        return jsonify({"error": "データの登録に失敗しました"}), 500

@app.route('/')
def index():
    camera_url = url_for('camera', _external=True)
    # スプレッドシートにカメラページのURLを追加
    try:
        sheet = client.open(os.getenv('SPREADSHEET_NAME')).worksheet('カメラ起動')
        sheet.update('A1', 'カメラ起動URL')
        sheet.update('B1', camera_url)
    except Exception as e:
        app.logger.error(f"Failed to update spreadsheet with camera URL: {str(e)}")
    
    app.logger.debug(f"Camera URL: {camera_url}")  # デバッグ用ログ
    return render_template('index.html', camera_url=camera_url)

@app.route('/static/<path:filename>')
def send_static(filename):
    try:
        app.logger.info(f"Attempting to send file: {filename}")
        full_path = os.path.join(app.static_folder, filename)
        app.logger.info(f"Full file path: {full_path}")
        app.logger.info(f"File exists: {os.path.exists(full_path)}")
        
        if not os.path.exists(full_path):
            app.logger.error(f"File not found: {full_path}")
            abort(404)
        
        return send_from_directory(app.static_folder, filename)
    except Exception as e:
        app.logger.error(f"Error sending file {filename}: {str(e)}")
        return str(e), 500  # エラーメッセージを返す

@app.after_request
def log_response_info(response):
    app.logger.debug('Response Status: %s', response.status)
    if response.content_type.startswith('text'):
        app.logger.debug('Response: %s', response.get_data(as_text=True))
    else:
        app.logger.debug('Response: [Binary data]')
    return response

@app.after_request
def log_response_info(response):
    app.logger.debug('Response Status: %s', response.status)
    if response.content_type.startswith('text'):
        app.logger.debug('Response: %s', response.get_data(as_text=True))
    else:
        app.logger.debug('Response: [Binary data]')
    return response

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=5000)