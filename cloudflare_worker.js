/**
 * Profile OS Webhook Mediator - Cloudflare Worker
 * ==============================================
 * Listens for incoming Telegram webhook updates, validates the chat ID,
 * and triggers the GitHub Actions daily prompt workflow immediately.
 * 
 * Environment Variables required in Cloudflare Worker settings:
 * - TELEGRAM_CHAT_ID: Your authorized Telegram chat ID (string)
 * - TELEGRAM_BOT_TOKEN: Your Telegram Bot Token
 * - GITHUB_TOKEN: A GitHub Personal Access Token (PAT) with 'actions' read/write scope
 * - GITHUB_REPO: Your GitHub repository name, e.g., "username/profile-os"
 * - GITHUB_REF: Optional, default is "main"
 */

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    try {
      const payload = await request.json();
      
      const message = payload.message || payload.edited_message;
      if (!message || !message.chat || !message.text) {
        return new Response("OK", { status: 200 }); // Ignore non-text updates
      }

      const chatId = String(message.chat.id);
      const authorizedChatId = String(env.TELEGRAM_CHAT_ID);

      if (chatId !== authorizedChatId) {
        console.warn(`Unauthorized Chat ID: ${chatId}`);
        return new Response("Unauthorized", { status: 403 });
      }

      const text = message.text.trim();
      const isCommand = text.startsWith("/");

      // Provide immediate visual feedback for commands on Telegram
      if (isCommand) {
        const cmd = text.split(/\s+/)[0].toLowerCase();
        let feedback = "";
        
        if (cmd === "/summary" || cmd === "/status") {
          feedback = "📊 Generating your weekly brief. Please wait about 30 seconds...";
        } else if (cmd === "/nudge") {
          feedback = "⏰ Generating a new daily nudge. Please wait...";
        } else if (cmd === "/help") {
          feedback = "⚙️ Fetching help menu...";
        }

        if (feedback) {
          await sendTelegramMessage(env.TELEGRAM_BOT_TOKEN, chatId, feedback);
        }
      }

      // Trigger GitHub Actions workflow_dispatch for daily.yml
      const repo = env.GITHUB_REPO; // "username/repo"
      const token = env.GITHUB_TOKEN;
      const ref = env.GITHUB_REF || "main";

      const ghUrl = `https://api.github.com/repos/${repo}/actions/workflows/daily.yml/dispatches`;
      
      console.log(`Triggering workflow_dispatch at: ${ghUrl}`);
      const ghResponse = await fetch(ghUrl, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Accept": "application/vnd.github.v3+json",
          "User-Agent": "ProfileOS-Cloudflare-Worker",
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ ref })
      });

      if (!ghResponse.ok) {
        const errText = await ghResponse.text();
        console.error(`GitHub API Error: ${ghResponse.status} - ${errText}`);
        await sendTelegramMessage(
          env.TELEGRAM_BOT_TOKEN, 
          chatId, 
          `❌ Failed to trigger workflow. GitHub response: ${ghResponse.status} ${ghResponse.statusText}`
        );
        return new Response("GitHub Trigger Failed", { status: 500 });
      }

      console.log("GitHub workflow triggered successfully.");
      return new Response("OK", { status: 200 });

    } catch (err) {
      console.error(`Error processing webhook: ${err}`);
      return new Response("Internal Server Error", { status: 500 });
    }
  }
};

async function sendTelegramMessage(botToken, chatId, text) {
  const url = `https://api.telegram.org/bot${botToken}/sendMessage`;
  try {
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text: text,
        parse_mode: "Markdown"
      })
    });
  } catch (err) {
    console.error(`Failed to send Telegram message: ${err}`);
  }
}
