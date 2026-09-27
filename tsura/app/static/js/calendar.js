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
  const timeFormatter = new Intl.DateTimeFormat(english, {
    hour: "2-digit", minute: "2-digit", hourCycle: "h23"
  });
  document.querySelectorAll("[data-calendar-zone]").forEach((element) => {
    element.textContent = zone;
  });
  document.querySelectorAll("[data-local-time]").forEach((element) => {
    const instant = new Date(element.dataset.localTime);
    if (!Number.isNaN(instant.getTime())) {
      element.textContent = element.hasAttribute("data-calendar-time-only")
        ? timeFormatter.format(instant)
        : element.hasAttribute("data-calendar-compact-time")
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

  const monthCalendar = document.querySelector("[data-month-calendar]");
  if (!monthCalendar) return;

  const dateKey = (date) => [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0")
  ].join("-");
  const todayKey = dateKey(new Date());
  const localMonth = todayKey.slice(0, 7);
  const todayLink = monthCalendar.querySelector("[data-calendar-today]");
  const todayUrl = new URL(todayLink.href);
  todayUrl.searchParams.set("month", localMonth);
  todayLink.href = todayUrl.toString();

  if (monthCalendar.dataset.monthIsCurrent === "true" &&
      monthCalendar.dataset.monthKey !== localMonth) {
    window.location.replace(todayLink.href);
    return;
  }

  const days = new Map();
  monthCalendar.querySelectorAll("[data-month-day]").forEach((cell) => {
    days.set(cell.dataset.monthDay, cell.querySelector("[data-month-day-events]"));
    if (cell.dataset.monthDay === todayKey) cell.classList.add("is-today");
  });
  const overflow = monthCalendar.querySelector("[data-month-overflow]");
  [...monthCalendar.querySelectorAll("[data-month-event]")]
    .sort((a, b) => new Date(a.dataset.eventStart) - new Date(b.dataset.eventStart))
    .forEach((event) => {
      const instant = new Date(event.dataset.eventStart);
      if (Number.isNaN(instant.getTime())) return;
      (days.get(dateKey(instant)) || overflow).appendChild(event);
    });
});
