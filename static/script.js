const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

function setupWaveAnimation(section) {
    const canvas = section.querySelector(".wave-canvas");

    if (!(canvas instanceof HTMLCanvasElement)) {
        return;
    }

    const context = canvas.getContext("2d");
    if (!context) {
        console.error("Could not initialize a wave animation.");
        return;
    }

    const pointer = { x: 0, y: 0, movedAt: -Infinity };
    let width = 0;
    let height = 0;
    let frameId = 0;
    let startTime = performance.now();

    function drawFrame(now) {
        const time = reducedMotion.matches ? 0 : (now - startTime) * 0.00035;
        const spacing = width < 620 ? 21 : 18;
        const dots = [[], [], []];
        const cursorAge = Number.isFinite(pointer.movedAt) ? (now - pointer.movedAt) * 0.001 : Infinity;
        const cursorStrength = Math.max(0, 1 - cursorAge / 1.1);

        context.clearRect(0, 0, width, height);

        for (let y = spacing / 2; y < height; y += spacing) {
            for (let x = spacing / 2; x < width; x += spacing) {
                const wave =
                    Math.sin(x * 0.008 + time + Math.sin(y * 0.006 - time) * 1.4) * 23 +
                    Math.sin(y * 0.012 + time * 0.8) * 9;
                const distance = Math.hypot(x - pointer.x, y - pointer.y);
                const ripple = cursorStrength > 0
                    ? Math.sin(distance * 0.065 - cursorAge * 8) *
                      Math.exp(-distance / 135) *
                      cursorStrength *
                      20
                    : 0;
                const displacedY = y + wave + ripple;
                const edgeFade = Math.min(1, x / 150, (width - x) / 150, y / 100, (height - y) / 100);
                const brightness = Math.max(0, Math.min(0.95, (0.28 + Math.cos(wave * 0.055) * 0.3) * edgeFade));
                const group = Math.min(2, Math.floor(brightness * 3));

                dots[group].push([x, displacedY, 0.75 + brightness * 1.1]);
            }
        }

        const opacities = [0.2, 0.38, 0.62];
        for (let group = 0; group < dots.length; group += 1) {
            context.beginPath();
            for (const [x, y, radius] of dots[group]) {
                context.moveTo(x + radius, y);
                context.arc(x, y, radius, 0, Math.PI * 2);
            }
            context.fillStyle = `rgba(154, 202, 241, ${opacities[group]})`;
            context.fill();
        }
    }

    function animate(now) {
        drawFrame(now);
        if (!reducedMotion.matches && !document.hidden) {
            frameId = window.requestAnimationFrame(animate);
        } else {
            frameId = 0;
        }
    }

    function startAnimation() {
        if (frameId || reducedMotion.matches || document.hidden) {
            return;
        }
        startTime = performance.now();
        frameId = window.requestAnimationFrame(animate);
    }

    function stopAnimation() {
        if (frameId) {
            window.cancelAnimationFrame(frameId);
            frameId = 0;
        }
        drawFrame(performance.now());
    }

    function resizeCanvas() {
        const bounds = section.getBoundingClientRect();
        const pixelRatio = Math.min(window.devicePixelRatio || 1, 1.5);

        width = bounds.width;
        height = bounds.height;
        canvas.width = Math.round(width * pixelRatio);
        canvas.height = Math.round(height * pixelRatio);
        context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);

        if (reducedMotion.matches || document.hidden) {
            drawFrame(performance.now());
        }
    }

    section.addEventListener("pointermove", (event) => {
        const bounds = section.getBoundingClientRect();
        pointer.x = event.clientX - bounds.left;
        pointer.y = event.clientY - bounds.top;
        pointer.movedAt = performance.now();
        startAnimation();
    });

    section.addEventListener("pointerleave", () => {
        pointer.movedAt = -Infinity;
    });

    new ResizeObserver(resizeCanvas).observe(section);
    resizeCanvas();
    startAnimation();
    return { startAnimation, stopAnimation };
}

const animations = [...document.querySelectorAll(".hero, .moments")]
    .map(setupWaveAnimation)
    .filter(Boolean);

document.addEventListener("visibilitychange", () => {
    for (const animation of animations) {
        if (document.hidden) {
            animation.stopAnimation();
        } else {
            animation.startAnimation();
        }
    }
});

reducedMotion.addEventListener("change", () => {
    for (const animation of animations) {
        if (reducedMotion.matches) {
            animation.stopAnimation();
        } else {
            animation.startAnimation();
        }
    }
});
