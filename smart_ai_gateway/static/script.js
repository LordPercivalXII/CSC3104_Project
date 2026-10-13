
const chatForm = document.getElementById("chat-form");
const promptInput = document.getElementById("prompt-input");
const chatBox = document.getElementById("chat-box");
const sendButton = document.getElementById("send-button");

// Add a message to the chat interface
function addMessage(text, sender) {
    const message = document.createElement("div");

    message.classList.add("message", sender);
    message.textContent = text;

    chatBox.appendChild(message);
    chatBox.scrollTop = chatBox.scrollHeight;
}

// Handle form submission
chatForm.addEventListener("submit", async (event) => {
    // Prevent the page from reloading
    event.preventDefault();

    const prompt = promptInput.value.trim();

    if (!prompt) {
        return;
    }

    // Display the user's prompt
    addMessage(prompt, "user");

    // Clear and disable input while waiting
    promptInput.value = "";
    promptInput.disabled = true;
    sendButton.disabled = true;

    try {
        // Send the POST request to FastAPI
        const response = await fetch("/chat", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                prompt: prompt
            })
        });

        if (!response.ok) {
            throw new Error(`HTTP error: ${response.status}`);
        }

        // Convert JSON response to JavaScript object
        const data = await response.json();

        // Display the backend's response
        // You asked <prompt>
        addMessage(data.response, "assistant");

    } catch (error) {
        console.error("Request failed:", error);

        addMessage(
            "Sorry, something went wrong. Please try again.",
            "assistant"
        );
    } finally {
        // Re-enable the input
        promptInput.disabled = false;
        sendButton.disabled = false;
        promptInput.focus();
    }
});
