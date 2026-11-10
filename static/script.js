const chatForm = document.getElementById("chat-form");
const promptInput = document.getElementById("prompt-input");
const chatBox = document.getElementById("chat-box");
const sendButton = document.getElementById("send-button");
const newChatButton = document.getElementById("new-chat-button");
const settingsButton = document.getElementById("settings-button");
const settingsPanel = document.getElementById("settings");
const nameInput = document.getElementById("name-input");

const SUGGESTIONS = [
    "What is our leave policy?",
    "How do I reset my email password?",
    "Summarise the benefits of cloud computing",
];

// ---- who is asking -------------------------------------------------------
// Each browser gets a random guest name so the fair-share queue can tell people apart.
// The name can be changed under Settings and is remembered on this device.

function load(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
}

function save(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* storage blocked: fine */ }
}

function randomGuestName() {
    return "guest-" + Math.random().toString(36).slice(2, 7);
}

let userId = load("gw-user") || randomGuestName();
save("gw-user", userId);

nameInput.value = userId;

nameInput.addEventListener("change", () => {
    userId = nameInput.value.trim() || randomGuestName();
    nameInput.value = userId;
    save("gw-user", userId);
});

settingsButton.addEventListener("click", () => {
    const open = settingsPanel.hidden;
    settingsPanel.hidden = !open;
    settingsButton.setAttribute("aria-expanded", String(open));
});

// ---- chat UI -------------------------------------------------------------

function scrollToBottom() {
    chatBox.scrollTop = chatBox.scrollHeight;
}

// Add a message to the chat interface; returns the element
function addMessage(text, sender, extraClass) {
    const message = document.createElement("div");

    message.classList.add("message", sender);
    if (extraClass) {
        message.classList.add(extraClass);
    }
    message.textContent = text;

    chatBox.appendChild(message);
    scrollToBottom();
    return message;
}

// Plain-language note shown under an answer
function describeSource(data) {
    if (data.source === "semantic_cache") {
        const score = (typeof data.similarity_score === "number")
            ? ` (match ${Math.round(data.similarity_score * 100)}%)` : "";
        return { text: "Instant answer: a similar question was answered before" + score, cls: "similar" };
    }
    return { text: "New answer", cls: "" };
}

function addBadge(message, data, seconds, prompt) {
    const info = describeSource(data);
    const badge = document.createElement("span");

    badge.classList.add("badge");
    if (info.cls) {
        badge.classList.add(info.cls);
    }
    badge.textContent = `${info.text} · ${seconds.toFixed(1)} s`;
    message.appendChild(badge);

    // Cached answers can be reported as wrong (POST /feedback removes them from the cache)
    if (data.source === "semantic_cache" && data.point_id) {
        badge.appendChild(feedbackLink(data.point_id, prompt));
    }
    scrollToBottom();
}

function feedbackLink(pointId, prompt) {
    const link = document.createElement("button");
    link.type = "button";
    link.textContent = "Wrong answer?";
    // Looks like a small link inside the badge (no stylesheet change needed)
    link.style.cssText = "margin-left:8px;background:none;border:none;padding:0;" +
        "color:inherit;text-decoration:underline;cursor:pointer;font:inherit;";

    link.addEventListener("click", async () => {
        link.disabled = true;
        try {
            const response = await fetch("/feedback", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ point_id: pointId, prompt: prompt })
            });
            // 404: someone else already reported it, which is fine
            link.textContent = (response.ok || response.status === 404)
                ? "Thanks, it won't be shown again" : "Could not send, try again";
            link.disabled = response.ok || response.status === 404;
        } catch (error) {
            console.error("Feedback failed:", error);
            link.textContent = "Could not send, try again";
            link.disabled = false;
        }
    });
    return link;
}

function addTyping() {
    const message = document.createElement("div");

    message.classList.add("message", "assistant");
    message.setAttribute("aria-label", "Assistant is typing");
    message.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';

    chatBox.appendChild(message);
    scrollToBottom();
    return message;
}

function showWelcome() {
    chatBox.innerHTML = "";
    addMessage("Hello! How can I help you today?", "assistant");

    const row = document.createElement("div");
    row.classList.add("suggestions");

    SUGGESTIONS.forEach((text) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = text;
        button.addEventListener("click", () => sendPrompt(text));
        row.appendChild(button);
    });

    chatBox.appendChild(row);
}

function friendlyError(status) {
    if (status === 422) {
        return "That message could not be read. Please check it is not empty or too long.";
    }
    if (status >= 500) {
        return "The assistant is busy or restarting. Please try again in a moment.";
    }
    return "Sorry, something went wrong. Please try again.";
}

// ---- sending -------------------------------------------------------------

let busy = false;

async function sendPrompt(prompt) {
    prompt = prompt.trim();

    if (!prompt || busy) {
        return;
    }

    busy = true;

    // Suggestions are only for the first message
    const suggestions = chatBox.querySelector(".suggestions");
    if (suggestions) {
        suggestions.remove();
    }

    addMessage(prompt, "user");

    // Clear and disable input while waiting
    promptInput.value = "";
    autoGrow();
    promptInput.disabled = true;
    sendButton.disabled = true;

    const typing = addTyping();
    const started = performance.now();

    try {
        const body = { prompt: prompt, user_id: userId };

        const response = await fetch("/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        });

        if (!response.ok) {
            throw new Error(String(response.status));
        }

        const data = await response.json();
        const seconds = (performance.now() - started) / 1000;

        typing.remove();
        const message = addMessage(data.response, "assistant");
        addBadge(message, data, seconds, prompt);

    } catch (error) {
        console.error("Request failed:", error);

        typing.remove();
        const status = parseInt(error.message, 10);
        addMessage(friendlyError(Number.isNaN(status) ? 0 : status), "assistant", "error");
    } finally {
        busy = false;
        promptInput.disabled = false;
        sendButton.disabled = false;
        promptInput.focus();
    }
}

chatForm.addEventListener("submit", (event) => {
    // Prevent the page from reloading
    event.preventDefault();
    sendPrompt(promptInput.value);
});

// Enter sends, Shift+Enter adds a new line
promptInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        chatForm.requestSubmit();
    }
});

function autoGrow() {
    promptInput.style.height = "auto";
    promptInput.style.height = Math.min(promptInput.scrollHeight, 140) + "px";
}

promptInput.addEventListener("input", autoGrow);

newChatButton.addEventListener("click", () => {
    if (!busy) {
        showWelcome();
        promptInput.focus();
    }
});

showWelcome();
