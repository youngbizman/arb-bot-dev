import logging
import os
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GITHUB_TOKEN = os.getenv("GH_DISPATCH_TOKEN", "").strip()
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "youngbizman").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "arb-bot-dev").strip()

# FIXED: Updated to match your actual .yml filenames in your workflows folder
WORKFLOWS = {
    "nba": "nba-bot.yml",
    "soccer": "soccer-bot.yml",
    "ufc": "ufc-bot.yml"
}

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class TelegramController:
    def __init__(self):
        self.headers = {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"token {GITHUB_TOKEN}"
        }

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🤖 Arbitrage Controller Active.\n\n"
            "Use these commands to trigger scans:\n"
            "/run_nba - Start NBA Sniper\n"
            "/run_soccer - Start Soccer Sniper\n"
            "/run_ufc - Start UFC Sniper"
        )

    async def trigger_workflow(self, update: Update, sport: str):
        workflow_file = WORKFLOWS.get(sport)
        url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/{workflow_file}/dispatches"
        data = {"ref": "main"}

        try:
            # This triggers the "workflow_dispatch" in your .yml files
            response = requests.post(url, headers=self.headers, json=data)
            response.raise_for_status()
            await update.message.reply_text(f"🚀 Success! GitHub is now starting the {sport.upper()} scanner.")
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to trigger GitHub: {str(e)}")

    async def run_nba(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.trigger_workflow(update, "nba")

    async def run_soccer(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.trigger_workflow(update, "soccer")

    async def run_ufc(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.trigger_workflow(update, "ufc")

if __name__ == "__main__":
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Missing TELEGRAM_TOKEN")
    if not GITHUB_TOKEN:
        raise RuntimeError("Missing GH_DISPATCH_TOKEN")

    controller = TelegramController()
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Commands registered in your bot
    application.add_handler(CommandHandler("start", controller.start))
    application.add_handler(CommandHandler("run_nba", controller.run_nba))
    application.add_handler(CommandHandler("run_soccer", controller.run_soccer))
    application.add_handler(CommandHandler("run_ufc", controller.run_ufc))

    print("✅ Telegram Controller is listening for commands...")
    application.run_polling()
