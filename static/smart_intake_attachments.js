(() => {
  const form = document.querySelector("[data-smart-intake-upload]");
  if (form && typeof DataTransfer !== "undefined") {
    const input = form.querySelector("[data-smart-intake-files]");
    const drop = form.querySelector("[data-smart-intake-drop]");
    const previews = form.querySelector("[data-smart-intake-previews]");
    let files = [];

    const key = (file) => `${file.name}:${file.size}:${file.lastModified}`;
    const sync = () => {
      const transfer = new DataTransfer();
      files.forEach((file) => transfer.items.add(file));
      input.files = transfer.files;
      previews.replaceChildren();
      files.forEach((file, index) => {
        const row = document.createElement("div");
        row.className = "smart-intake-preview smart-attachment-row";
        const pdf = file.type === "application/pdf";
        const preview = document.createElement(pdf ? "a" : "button");
        const objectUrl = URL.createObjectURL(file);
        preview.className = "smart-attachment-preview";
        if (pdf) {
          preview.className = "button ghost compact";
          preview.href = objectUrl;
          preview.target = "_blank";
          preview.rel = "noopener";
          preview.textContent = "Open PDF";
        } else {
          preview.type = "button";
          preview.dataset.imagePreview = "";
          preview.dataset.imageName = file.name;
          preview.setAttribute("aria-label", `Preview ${file.name}`);
          const image = document.createElement("img");
          image.alt = "";
          image.src = objectUrl;
          preview.dataset.imageSrc = image.src;
          preview.append(image);
        }
        const name = document.createElement("span");
        name.textContent = file.name;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "button ghost";
        remove.textContent = "Remove";
        remove.setAttribute("aria-label", `Remove ${file.name}`);
        remove.addEventListener("click", () => {
          URL.revokeObjectURL(objectUrl);
          files.splice(index, 1);
          sync();
        });
        row.append(preview, name, remove);
        previews.append(row);
      });
    };
    const add = (incoming) => {
      const known = new Set(files.map(key));
      [...incoming].forEach((file) => {
        if ((file.type === "image/jpeg" || file.type === "image/png" || file.type === "application/pdf") && !known.has(key(file))) {
          files.push(file); known.add(key(file));
        }
      });
      sync();
    };
    input.addEventListener("change", () => add(input.files));
    ["dragenter", "dragover"].forEach((event) => drop.addEventListener(event, (e) => { e.preventDefault(); drop.classList.add("is-dragging"); }));
    ["dragleave", "drop"].forEach((event) => drop.addEventListener(event, (e) => { e.preventDefault(); drop.classList.remove("is-dragging"); }));
    drop.addEventListener("drop", (event) => add(event.dataTransfer.files));
  }

  const modal = document.querySelector("[data-image-modal]");
  if (!modal) return;
  const modalImage = modal.querySelector("[data-image-modal-image]");
  const modalName = modal.querySelector("[data-image-modal-name]");
  const previous = modal.querySelector("[data-image-modal-previous]");
  const next = modal.querySelector("[data-image-modal-next]");
  const closeButton = modal.querySelector("[data-image-modal-close]");
  let triggers = [];
  let current = 0;
  let returnFocus = null;

  const show = (index) => {
    triggers = [...document.querySelectorAll("[data-image-preview]")];
    if (!triggers.length) return;
    current = (index + triggers.length) % triggers.length;
    const trigger = triggers[current];
    modalImage.src = trigger.dataset.imageSrc;
    modalImage.alt = trigger.dataset.imageName;
    modalName.textContent = trigger.dataset.imageName;
    previous.hidden = next.hidden = triggers.length < 2;
  };
  const open = (trigger) => {
    returnFocus = trigger;
    triggers = [...document.querySelectorAll("[data-image-preview]")];
    show(triggers.indexOf(trigger));
    modal.hidden = false;
    closeButton.focus();
  };
  const close = () => {
    modal.hidden = true;
    modalImage.src = "";
    if (returnFocus) returnFocus.focus();
  };
  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-image-preview]");
    if (trigger) open(trigger);
  });
  closeButton.addEventListener("click", close);
  modal.addEventListener("click", (event) => { if (event.target === modal) close(); });
  previous.addEventListener("click", () => show(current - 1));
  next.addEventListener("click", () => show(current + 1));
  document.addEventListener("keydown", (event) => {
    if (modal.hidden) return;
    if (event.key === "Escape") close();
    else if (event.key === "ArrowLeft") show(current - 1);
    else if (event.key === "ArrowRight") show(current + 1);
  });
})();
