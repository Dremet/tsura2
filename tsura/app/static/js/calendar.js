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

  const countdowns = [...document.querySelectorAll("[data-event-countdown]")];
  if (countdowns.length) {
    const updateCountdowns = () => {
      const now = Date.now();
      countdowns.forEach((element) => {
        const remaining = new Date(element.dataset.eventCountdown).getTime() - now;
        if (Number.isNaN(remaining)) return;
        const prefix = element.querySelector(".calendar-countdown-prefix");
        const value = element.querySelector("[data-countdown-value]");
        const unit = element.querySelector("[data-countdown-unit]");
        element.classList.toggle("is-started", remaining <= 0);
        if (remaining <= 0) {
          prefix.textContent = "";
          value.textContent = "Started";
          unit.textContent = "";
          element.setAttribute("aria-label", "Race has started");
          return;
        }
        prefix.textContent = "In";
        if (remaining < 60 * 60 * 1000) {
          value.textContent = "<1";
          unit.textContent = "hour";
          element.setAttribute("aria-label", "Race starts in less than one hour");
          return;
        }
        const isHours = remaining < 24 * 60 * 60 * 1000;
        const amount = Math.ceil(remaining / (isHours ? 60 * 60 * 1000 : 24 * 60 * 60 * 1000));
        const label = isHours ? "hour" : "day";
        value.textContent = String(amount);
        unit.textContent = amount === 1 ? label : `${label}s`;
        element.setAttribute("aria-label", `Race starts in ${amount} ${unit.textContent}`);
      });
    };
    updateCountdowns();
    setInterval(updateCountdowns, 60 * 1000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) updateCountdowns();
    });
  }

  const calendarGrid = document.querySelector("[data-month-calendar], [data-week-calendar]");
  if (!calendarGrid) return;

  const dateKey = (date) => [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0")
  ].join("-");
  const now = new Date();
  const todayKey = dateKey(now);
  const localMonth = todayKey.slice(0, 7);
  const localMonday = new Date(now.getFullYear(), now.getMonth(),
    now.getDate() - (now.getDay() + 6) % 7);
  const localWeek = dateKey(localMonday);
  const isMonth = calendarGrid.hasAttribute("data-month-calendar");
  const selectionKey = isMonth ? localMonth : localWeek;
  const param = isMonth ? "month" : "week";
  const todayLink = calendarGrid.querySelector("[data-calendar-today]");
  const todayUrl = new URL(todayLink.href);
  todayUrl.searchParams.set(param, selectionKey);
  todayLink.href = todayUrl.toString();

  const isCurrent = isMonth ? calendarGrid.dataset.monthIsCurrent : calendarGrid.dataset.weekIsCurrent;
  const serverKey = isMonth ? calendarGrid.dataset.monthKey : calendarGrid.dataset.weekKey;
  if (isCurrent === "true" && serverKey !== selectionKey) {
    window.location.replace(todayLink.href);
    return;
  }

  const days = new Map();
  const cells = new Map();
  calendarGrid.querySelectorAll("[data-calendar-day]").forEach((cell) => {
    const key = cell.dataset.calendarDay;
    cells.set(key, cell);
    days.set(key, cell.querySelector("[data-calendar-day-events]"));
    cell.classList.toggle("is-today", key === todayKey);
    const dayNumber = cell.querySelector("[data-calendar-day-number]");
    if (key === todayKey) {
      dayNumber.setAttribute("aria-current", "date");
    } else {
      dayNumber.removeAttribute("aria-current");
    }
  });
  const overflow = calendarGrid.querySelector("[data-calendar-overflow]");
  [...calendarGrid.querySelectorAll("[data-calendar-grid-event]")]
    .sort((a, b) => new Date(a.dataset.eventStart) - new Date(b.dataset.eventStart))
    .forEach((event) => {
      const instant = new Date(event.dataset.eventStart);
      if (Number.isNaN(instant.getTime())) return;
      (days.get(dateKey(instant)) || overflow).appendChild(event);
    });

  if (!isMonth) return;

  const agenda = calendarGrid.querySelector("[data-month-agenda]");
  const agendaHeading = agenda.querySelector("[data-month-agenda-heading]");
  const agendaEvents = agenda.querySelector("[data-month-agenda-events]");
  const agendaEmpty = agenda.querySelector("[data-month-agenda-empty]");
  const fullDate = new Intl.DateTimeFormat(english, {
    weekday: "long", day: "numeric", month: "long", year: "numeric"
  });
  let agendaInitialized = false;
  const selectDay = (key) => {
    cells.forEach((cell, cellKey) => {
      const selected = cellKey === key;
      cell.classList.toggle("is-selected", selected);
      cell.querySelector("[data-month-select-day]").setAttribute("aria-pressed", String(selected));
    });
    if (window.bootstrap?.Tooltip) {
      agendaEvents.querySelectorAll("[data-bs-toggle='tooltip']").forEach((button) => {
        window.bootstrap.Tooltip.getInstance(button)?.dispose();
      });
    }
    const sourceEvents = [...days.get(key).querySelectorAll("[data-calendar-grid-event]")];
    agendaEvents.replaceChildren(...sourceEvents.map((event) => event.cloneNode(true)));
    agendaEmpty.hidden = sourceEvents.length > 0;
    agendaHeading.textContent = fullDate.format(new Date(`${key}T12:00:00`));
    if (agendaInitialized && window.bootstrap?.Tooltip) {
      agendaEvents.querySelectorAll("[data-bs-toggle='tooltip']").forEach((button) => {
        window.bootstrap.Tooltip.getOrCreateInstance(button);
      });
    }
  };

  cells.forEach((cell, key) => {
    const count = days.get(key).querySelectorAll("[data-calendar-grid-event]").length;
    cell.classList.toggle("has-events", count > 0);
    const button = cell.querySelector("[data-month-select-day]");
    button.querySelector("[data-month-day-count]").textContent = count > 1 ? String(count) : "•";
    button.setAttribute("aria-label", `${button.getAttribute("aria-label")}; ${count} ${count === 1 ? "race" : "races"}`);
    button.addEventListener("click", () => selectDay(key));
  });
  const monthCells = [...cells.values()].filter((cell) =>
    cell.dataset.calendarDay.startsWith(calendarGrid.dataset.monthKey));
  const initialCell = monthCells.find((cell) => cell.dataset.calendarDay === todayKey) ||
    monthCells.find((cell) => cell.classList.contains("has-events")) || monthCells[0];
  selectDay(initialCell.dataset.calendarDay);
  agenda.hidden = false;
  calendarGrid.classList.add("is-enhanced");
  agendaInitialized = true;
});
