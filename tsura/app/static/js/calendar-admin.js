document.addEventListener("DOMContentLoaded", () => {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const $ = (id) => document.getElementById(id);
  $("event-timezone").value = zone;
  $("calendar-zone").textContent = zone;

  const localInput = (date) => {
    const pad = (n) => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  };
  const initial = new Date();
  initial.setHours(initial.getHours() + 1, 0, 0, 0);
  $("event-start").value = localInput(initial);
  let editOriginal = null;

  const syncRepeat = () => {
    const enabled = $("event-action").value === "create_event" &&
      $("event-league").value !== "one-off";
    $("repeat-wrap").classList.toggle("d-none", !enabled);
    $("repeat-count").disabled = !enabled;
    if (!enabled) $("repeat-count").value = "1";
  };
  $("event-league").addEventListener("change", syncRepeat);
  syncRepeat();

  $("event-form").addEventListener("submit", () => {
    const local = $("event-start").value;
    // Keep the later DST occurrence if an existing ambiguous time was not changed.
    const chosen = editOriginal && local === editOriginal.local
      ? new Date(editOriginal.start) : new Date(local);
    $("event-offset").value = Number.isNaN(chosen.getTime()) ? "" : String(-chosen.getTimezoneOffset());
  });

  const cancelEvent = () => {
    $("event-form").reset();
    $("event-action").value = "create_event";
    $("event-id").value = "";
    editOriginal = null;
    $("event-start").value = localInput(initial);
    $("event-timezone").value = zone;
    $("event-form-title").textContent = "Add race event";
    $("event-submit").textContent = "Create event(s)";
    syncRepeat();
    $("cancel-event-edit").classList.add("d-none");
  };
  $("cancel-event-edit").addEventListener("click", cancelEvent);
  document.querySelectorAll(".edit-event").forEach((button) => {
    button.addEventListener("click", () => {
      $("event-action").value = "update_event";
      $("event-id").value = button.dataset.id;
      $("event-league").value = button.dataset.league;
      $("event-details").value = button.dataset.details;
      editOriginal = {start: button.dataset.start,
                      local: localInput(new Date(button.dataset.start))};
      $("event-start").value = editOriginal.local;
      $("event-form-title").textContent = "Edit race event";
      $("event-submit").textContent = "Save changes";
      syncRepeat();
      $("cancel-event-edit").classList.remove("d-none");
      $("event-form").scrollIntoView({behavior: "smooth", block: "start"});
    });
  });

  const cancelLeague = () => {
    $("league-form").reset();
    $("league-action").value = "create_league";
    $("league-id").value = "";
    $("league-form-title").textContent = "Add league";
    $("league-submit").textContent = "Create league";
    $("cancel-league-edit").classList.add("d-none");
  };
  $("cancel-league-edit").addEventListener("click", cancelLeague);
  document.querySelectorAll(".edit-league").forEach((button) => {
    button.addEventListener("click", () => {
      $("league-action").value = "update_league";
      $("league-id").value = button.dataset.id;
      $("league-name").value = button.dataset.name;
      $("league-description").value = button.dataset.description;
      document.querySelectorAll('input[name="color_key"]').forEach((input) => {
        input.checked = input.value === button.dataset.color;
      });
      $("league-form-title").textContent = "Edit league";
      $("league-submit").textContent = "Save changes";
      $("cancel-league-edit").classList.remove("d-none");
      $("league-form").scrollIntoView({behavior: "smooth", block: "start"});
    });
  });
});
