# 📱 Termux: premium emoji ids nikalne ka tarika

Ye script @OrderEmoji (ya koi bhi channel) ke saare **premium custom emoji ids**
ek saath nikalta hai, har id ko Telegram se **verify** karta hai (dead ids drop),
aur `utils/emoji_order_pack.py` regenerate kar deta hai — jise bot automatically
har plain unicode emoji ki jagah premium emoji dikhane ke liye use karta hai.

## 1. Termux setup
```bash
pkg update -y && pkg install -y python git
pip install -U pyrofork tgcrypto
git clone https://github.com/thomas82822/Melody_music
cd Melody_music
```

## 2. API credentials
https://my.telegram.org → API development tools → `api_id` / `api_hash`
```bash
export API_ID=123456
export API_HASH=xxxxxxxxxxxxxxxxxxxxxxxx
```

## 3. Run
```bash
python tools/termux_emoji_harvest.py --channel OrderEmoji
```
Pehli baar phone number + OTP maangega (apna **user** account, bot nahi — bot
channel history nahi padh sakta). Session `tools/melody_emoji.session` me save
hoti hai; use **kabhi commit mat karna**.

Options:
| flag | matlab |
|---|---|
| `--channel X` | aur channels add karo (repeat kar sakte ho) |
| `--limit 500` | sirf last 500 messages scan karo (default: sab) |
| `--print` | file likhne ke bajaye sirf `glyph -> id` print karo |
| `--out path` | alag output file |

## 4. Push
```bash
git add utils/emoji_order_pack.py
git commit -m "emoji: refresh premium ids"
git push
```
Deploy hote hi bot startup par har id dobara Telegram se resolve karta hai
(`utils/emoji_map.resolve_emoji_map`), to sirf working ids hi live jaati hain.
