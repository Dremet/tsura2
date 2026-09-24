document.addEventListener("DOMContentLoaded", () => {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const formatter = new Intl.DateTimeFormat(undefined, {
    weekday: "short", year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", timeZoneName: "short"
  });
  document.querySelectorAll("[data-local-time]").forEach((element) => {
    const instant = new Date(element.dataset.localTime);
    if (!Number.isNaN(instant.getTime())) {
      element.textContent = formatter.format(instant);
      element.setAttribute("title", zone);
    }
  });
});
