document.addEventListener("DOMContentLoaded", () => {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const formatter = new Intl.DateTimeFormat(undefined, {
    weekday: "short", year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", timeZoneName: "short"
  });
  const compactFormatter = new Intl.DateTimeFormat(undefined, {
    weekday: "short", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", timeZoneName: "short"
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
  const day = new Intl.DateTimeFormat(undefined, {day: "2-digit"});
  const month = new Intl.DateTimeFormat(undefined, {month: "short"});
  const weekday = new Intl.DateTimeFormat(undefined, {weekday: "short"});
  document.querySelectorAll("[data-calendar-date]").forEach((element) => {
    const instant = new Date(element.dataset.calendarDate);
    if (Number.isNaN(instant.getTime())) return;
    element.querySelector("[data-calendar-day]").textContent = day.format(instant);
    element.querySelector("[data-calendar-month]").textContent = month.format(instant);
    element.querySelector("[data-calendar-weekday]").textContent = weekday.format(instant);
  });
});
