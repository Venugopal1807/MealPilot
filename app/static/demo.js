"use strict";

const form = document.getElementById("meal-form");
const input = document.getElementById("meal-request");
const submit = document.getElementById("submit");
const output = document.getElementById("output");
const error = document.getElementById("error");
const status = document.getElementById("status");
const title = document.getElementById("response-title");
const panel = document.getElementById("response-panel");
const stateLabel = document.getElementById("result-state");

function setState(state, label) {
  panel.dataset.state = state;
  stateLabel.textContent = label;
}

// All request and API text is inserted as text, never HTML.
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function message(heading, description, issues = []) {
  const box = element("div", undefined, "message");
  box.append(element("h3", heading), element("p", description));
  if (issues.length) {
    const list = element("ul");
    issues.forEach(issue => list.append(element("li", issue.message)));
    box.append(list);
  }
  output.append(box);
}

function render(data) {
  if (data.status === "clarification_required" || data.status === "unsupported") {
    setState(data.status === "unsupported" ? "unsupported" : "clarification", data.status === "unsupported" ? "Outside this demo" : "Needs clarification");
    title.textContent = data.status === "unsupported" ? "This request needs a different approach." : "Let’s make your request clearer.";
    message("Review your request", "We haven’t selected any meals. Review the guidance below, edit your request and try again.", data.issues);
    return;
  }
  if (data.status !== "parsed") throw new Error("Unexpected response");
  const hard = data.intent.hard_constraints;
  const soft = data.intent.soft_preferences;
  const summary = element("div", undefined, "intent");
  summary.append(element("h3", "Your request, interpreted"));
  const facts = element("dl");
  const rows = [
    ["Budget cap", `${hard.budget} catalog units`],
    ["Must include", hard.required_ingredients.join(", ") || "None specified"],
    ["Must exclude", hard.avoid_ingredients.join(", ") || "None specified"],
    ["Preferences", [soft.preferred_cuisine, soft.want_spicy ? "spicy" : null].filter(Boolean).join(", ") || "None specified"],
  ];
  rows.forEach(([label, value]) => facts.append(element("dt", label), element("dd", value)));
  summary.append(facts, element("p", "Budget and ingredients filter results. Preferences affect ranking only.", "intent-note"));
  output.append(summary);
  const result = data.result;
  setState(result.count ? "parsed" : "empty", result.count ? "Matches found" : "No matches");
  title.textContent = result.count ? `${result.count} meal${result.count === 1 ? "" : "s"} fit your request.` : "No meals fit this request.";
  if (!result.count) {
    message("No matches in this catalog", "Your constraints were kept. You can edit the budget or ingredients and try again; requirements are never relaxed automatically.");
    return;
  }
  output.append(element("p", `Catalog source: ${result.source} · ordered by the recommendation service`, "result-meta"));
  const list = element("ol", undefined, "results");
  result.recommendations.forEach((meal, index) => {
    const item = element("li", undefined, "meal");
    const top = element("div", undefined, "meal-top");
    const name = element("div");
    name.append(element("h3", `${index + 1}. ${meal.dish}`), element("p", meal.restaurant, "restaurant"));
    const price = element("div", String(meal.price), "price");
    price.append(element("small", "catalog units"));
    top.append(name, price);
    const tags = element("div", undefined, "tags");
    [meal.cuisine, meal.spicy ? "Spicy" : "Spicy flag: no", `Rank score ${meal.score}`].forEach(text => tags.append(element("span", text, "tag")));
    const reasons = element("ul", undefined, "reasons");
    meal.reasons.forEach(reason => reasons.append(element("li", reason)));
    item.append(top, tags, element("p", `Listed ingredients: ${meal.ingredients.join(", ")}`, "ingredients"), reasons);
    list.append(item);
  });
  output.append(list);
}

document.querySelectorAll("[data-example]").forEach(button => {
  button.addEventListener("click", () => {
    input.value = button.dataset.example;
    input.focus();
  });
});

form.addEventListener("submit", async event => {
  event.preventDefault();
  if (submit.disabled) return;
  error.hidden = true;
  error.textContent = "";
  input.setAttribute("aria-invalid", "false");
  output.replaceChildren();
  if (!input.value.trim() || input.value.length > 2000) {
    setState("error", "Check your request");
    input.setAttribute("aria-invalid", "true");
    title.textContent = "Your request needs some text.";
    status.textContent = "Request not submitted.";
    error.textContent = "Enter a meal request between 1 and 2000 characters.";
    error.hidden = false;
    input.focus();
    return;
  }
  submit.disabled = true;
  input.disabled = true;
  document.querySelectorAll("[data-example]").forEach(button => { button.disabled = true; });
  submit.textContent = "Finding meals…";
  panel.setAttribute("aria-busy", "true");
  setState("loading", "Checking your request");
  title.textContent = "Checking your request…";
  status.textContent = "Interpreting your request and checking the catalog.";
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch("/recommend/text", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text: input.value}), signal: controller.signal,
    });
    if (!response.ok) {
      if (response.status === 422) input.setAttribute("aria-invalid", "true");
      const messages = {
        422: "The request could not be validated. Use nonblank text of at most 2000 characters.",
        502: "The service could not validate its data. Please try again later.",
        503: "Meal recommendations are temporarily unavailable. Please try again later.",
        504: "The service timed out. Please try again.",
      };
      throw new Error(messages[response.status] || "The service could not complete your request. Please try again later.");
    }
    render(await response.json());
    status.textContent = "Request complete.";
  } catch (failure) {
    setState("error", "Request not completed");
    output.replaceChildren();
    title.textContent = "We couldn’t complete this request.";
    // Never display raw HTTP bodies or untrusted exception messages.
    const safeMessages = ["The request could not be validated. Use nonblank text of at most 2000 characters.", "The service could not validate its data. Please try again later.", "Meal recommendations are temporarily unavailable. Please try again later.", "The service timed out. Please try again.", "The service could not complete your request. Please try again later."];
    error.textContent = failure.name === "AbortError" ? "The request took too long. Please try again. A local timeout does not cancel server work." : safeMessages.includes(failure.message) ? failure.message : "Could not load recommendations. Check your connection and try again.";
    error.hidden = false;
    status.textContent = "Request failed.";
  } finally {
    clearTimeout(timeout);
    submit.disabled = false;
    input.disabled = false;
    document.querySelectorAll("[data-example]").forEach(button => { button.disabled = false; });
    submit.textContent = "Find my meal →";
    panel.setAttribute("aria-busy", "false");
    title.focus();
  }
});
