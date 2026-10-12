# AlterChat 💬

**AlterChat** is a desktop application that lets you create offline AI clones of your friends based on exported WhatsApp chat logs and chat with them in an interface that closely mimics WhatsApp Web and Telegram Desktop.

All inference runs **100% locally** using `llama-cpp-python` and small, memory-efficient models (like **Qwen 2.5 0.5B Instruct GGUF**). Your chat history, personal data, and photos never leave your machine.

---

## 🚀 Quick Setup & Installation

> [!IMPORTANT]
> **Why `setup.bat` instead of a plain `pip install -r requirements.txt`?**  
> On Windows, installing `llama-cpp-python` from PyPI usually attempts to build C++ binaries from source, requiring gigabytes of Microsoft Visual Studio C++ Build Tools and CMake.  
> The provided `setup.bat` script installs a pre-built binary CPU wheel directly, bypassing any C++ compiler requirements completely.

### Windows (Recommended)
1. Ensure you have **Python 3.10, 3.11, or 3.12** installed (ensure *"Add Python to PATH"* was checked during Python installation).
2. **Double-click `setup.bat`** (or run it via Command Prompt / PowerShell):
   ```cmd
   setup.bat
   ```
3. Once complete, double-click **`run.bat`** to start AlterChat.

---

### macOS & Linux
1. Open a terminal in the project directory.
2. Make the script executable and run:
   ```bash
   chmod +x setup.sh
   ./setup.sh
   ```
3. Launch AlterChat:
   ```bash
   source .venv/bin/activate
   python main.py
   ```

---

### Manual Installation (Power Users)
If you prefer running commands manually in your own terminal or virtual environment:

```bash
# 1. (Optional) Create & activate virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 2. Install pre-compiled llama-cpp-python (Bypasses CMake on Windows)
pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu

# 3. Install remaining dependencies
pip install -r requirements.txt
```

---

## 🧠 AI Model (downloaded automatically)

`setup.bat` / `setup.sh` automatically download **`qwen2.5-0.5b-instruct-q4_k_m.gguf`** (~400 MB) from
[Qwen/Qwen2.5-0.5B-Instruct-GGUF](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF) into `models/`.

- If the download is interrupted, just run setup again (or `python download_model.py`) — it resumes and skips files that are already complete.
- Use `python download_model.py --force` to re-download.
- You can also pick any other GGUF model via **Settings (⚙️) > AI Model > Browse**.

---

## 📱 How to Use AlterChat

1. **First-Boot Terms & Conditions (EULA):**  
   On your first launch, AlterChat will display a data privacy agreement explaining that all processing happens locally. Check *"I agree to the Terms and Conditions"* and click **Continue**.

2. **Add a Contact:**  
   - Click the **`+`** button in the sidebar header.
   - Enter your friend's name (e.g. `Budi`).
   - *(Optional)* Upload a profile photo (automatically converted into a circular avatar).
   - Upload a WhatsApp export file (`.txt` or `.zip`).
   - Select which sender is your friend (AlterChat automatically matches names).
   - Click **Save**. AlterChat extracts your friend's unique slang, sentence lengths, and emojis to build their persona prompt, and loads past conversations into memory.

3. **Start Chatting:**  
   Click the contact in the sidebar and start typing! AlterChat utilizes a lightweight keyword RAG system that searches past chats for contextually relevant memories before generating replies.

---

## 📦 Building a Portable App (`--onedir`)

Double-click **`build.bat`** (after `setup.bat`). It runs:

```powershell
python -m PyInstaller main.py --noconfirm --clean --onedir --windowed --name "AlterChat" `
    --collect-all customtkinter --collect-all llama_cpp --hidden-import PIL._tkinter_finder
```

and then places `models\` and an empty `data\` skeleton next to the executable:

```text
dist\AlterChat\
 ├── AlterChat.exe     # starts instantly (no self-extraction)
 ├── _internal\        # Python runtime + libraries
 ├── models\           # qwen2.5-0.5b-instruct-q4_k_m.gguf
 └── data\             # contacts, avatars, history (created at runtime)
```

Zip the whole `dist\AlterChat` folder to distribute it. Your personal `data\` is never copied into the build.


