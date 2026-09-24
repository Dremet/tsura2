document.addEventListener("DOMContentLoaded", () => {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const english = "en-GB";
  const formatter = new Intl.DateTimeFormat(english, {
    weekday: "short", year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZoneName: "short"
  });
  const compactFormatter = new Intl.DateTimeFormat(english, {
    weekday: "short", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZoneName: "short"
  });
  document.querySelectorAll("[data-calendar-zone]").forEach((element) => {
    element.textContent = zone;
  });
  document.querySelectorAll("[data-local-time]").forEach((element) => {
    const instant = new Date(element.dataset.localTime);
    if (!Number.isNaN(instant.getTime())) {
      element.textContent = element.hasAttribute("data-calendar-compact-time")
        ? compactFormatter.format(instant) : formatter.format(instant);
      element.setAttribute("title", zone);
    }
  });
  const day = new Intl.DateTimeFormat(english, {day: "2-digit"});
  const month = new Intl.DateTimeFormat(english, {month: "short"});
  const weekday = new Intl.DateTimeFormat(english, {weekday: "short"});
  document.querySelectorAll("[data-calendar-date]").forEach((element) => {
    const instant = new Date(element.dataset.calendarDate);
    if (Number.isNaN(instant.getTime())) return;
    element.querySelector("[data-calendar-day]").textContent = day.format(instant);
    element.querySelector("[data-calendar-month]").textContent = month.format(instant);
    element.querySelector("[data-calendar-weekday]").textContent = weekday.format(instant);
  });
});
