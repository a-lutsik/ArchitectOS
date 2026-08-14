// Voice memory capture (Web Speech API). Classic script loaded before app.js; shares global scope.
let voiceRecognition = null;
let voiceListening = false;
function voiceRecognitionLanguage() {
  return ({ en: "en-US", ru: "ru-RU", uk: "uk-UA", he: "he-IL" })[state.language] || "en-US";
}
function voiceStatus(message, tone = "") {
  const status = document.querySelector("#voice-status");
  if (!status) return;
  status.className = `provider-test ${tone}`.trim();
  status.textContent = message;
}
function setVoiceListening(listening) {
  voiceListening = listening;
  const start = document.querySelector("#voice-start");
  const stop = document.querySelector("#voice-stop");
  if (start) start.disabled = listening;
  if (stop) stop.disabled = !listening;
}
function speechRecognitionCtor() {
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}
function autoVoiceLabel(text) {
  return (text || "")
    .trim()
    .split(/\s+/)
    .slice(0, 8)
    .join(" ")
    .replace(/[.,;:!?]+$/, "");
}
function appendVoiceTranscript(text) {
  const transcript = document.querySelector("#voice-memory-text");
  const label = document.querySelector("#voice-memory-label");
  if (!transcript || !text) return;
  const spacer = transcript.value.trim() ? " " : "";
  transcript.value = `${transcript.value.trim()}${spacer}${text.trim()}`.trim();
  if (label && !label.value.trim()) label.value = autoVoiceLabel(transcript.value);
}
function ensureVoiceRecognition() {
  const Recognition = speechRecognitionCtor();
  if (!Recognition) return null;
  if (voiceRecognition) return voiceRecognition;
  voiceRecognition = new Recognition();
  voiceRecognition.continuous = true;
  voiceRecognition.interimResults = true;
  voiceRecognition.onstart = () => {
    setVoiceListening(true);
    voiceStatus("Listening...", "ok");
  };
  voiceRecognition.onend = () => {
    setVoiceListening(false);
    voiceStatus("Stopped");
  };
  voiceRecognition.onerror = event => {
    setVoiceListening(false);
    voiceStatus(`Voice error: ${event.error || "unknown"}`, "error");
  };
  voiceRecognition.onresult = event => {
    let finalText = "";
    let interimText = "";
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const text = event.results[index][0].transcript || "";
      if (event.results[index].isFinal) finalText += `${text} `;
      else interimText += text;
    }
    if (finalText.trim()) appendVoiceTranscript(finalText);
    voiceStatus(interimText.trim() ? `Listening: ${interimText.trim()}` : "Listening...", "ok");
  };
  return voiceRecognition;
}
function startVoiceMemory() {
  const recognition = ensureVoiceRecognition();
  if (!recognition) {
    voiceStatus("Voice input is not supported in this browser.", "error");
    const start = document.querySelector("#voice-start");
    if (start) start.disabled = true;
    return;
  }
  if (voiceListening) return;
  recognition.lang = voiceRecognitionLanguage();
  try {
    recognition.start();
  } catch (error) {
    voiceStatus(error.message || "Could not start voice input.", "error");
  }
}
function stopVoiceMemory() {
  if (voiceRecognition && voiceListening) voiceRecognition.stop();
}
function clearVoiceMemory() {
  setElementValue("#voice-memory-label", "");
  setElementValue("#voice-memory-text", "");
  voiceStatus(speechRecognitionCtor() ? "Ready" : "Voice input is not supported in this browser.", speechRecognitionCtor() ? "" : "error");
}
async function saveVoiceMemory(event) {
  event.preventDefault();
  const text = (document.querySelector("#voice-memory-text")?.value || "").trim();
  const labelInput = document.querySelector("#voice-memory-label");
  if (!text) throw new Error("Record or type a transcript first.");
  const label = (labelInput?.value || "").trim() || autoVoiceLabel(text) || "Voice memory";
  const payload = {
    project_id: state.projectId,
    label,
    type: document.querySelector("#voice-memory-type")?.value || "Note",
    scope: document.querySelector("#voice-memory-scope")?.value || "project",
    text,
  };
  voiceStatus("Saving voice memory...");
  await api("/api/memory", { method: "POST", body: JSON.stringify(payload) });
  document.querySelector("#voice-memory-form")?.reset();
  voiceStatus("Voice memory saved", "ok");
  await refreshWorkspace();
  await runSearch("voice memory");
  scheduleGraphLoad();
}
function initVoiceMemory() {
  const start = document.querySelector("#voice-start");
  if (!start) return;
  on(start, "click", startVoiceMemory);
  on("#voice-stop", "click", stopVoiceMemory);
  on("#voice-clear", "click", clearVoiceMemory);
  on("#voice-memory-form", "submit", event => saveVoiceMemory(event).catch(showError));
  if (!speechRecognitionCtor()) {
    start.disabled = true;
    voiceStatus("Voice input is not supported in this browser.", "error");
  }
}
