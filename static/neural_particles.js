/**
 * Neural Particles - Floating Bioluminescent Organisms
 * A canvas-based particle system for the Card View background
 *
 * Features:
 * - 30 particles with organic drift (Perlin noise simulation)
 * - 4 organism types matching interaction types (activates, inhibits, binds, regulates)
 * - Radial gradient rendering with breathing animations
 * - Wraps around edges for infinite space feel
 * - 60fps performance target
 */

const NeuralParticles = (function() {
    'use strict';

    // --- Private State ---
    let canvas, ctx;
    let particles = [];
    let animationId = null;
    let lastTime = 0;

    // --- Configuration ---
    const CONFIG = {
        particleCount: 30,
        minSize: 1,
        maxSize: 3,
        minOpacity: 0.2,
        maxOpacity: 0.7,
        driftSpeed: 0.3, // Base drift speed
        types: ['activates', 'inhibits', 'binds', 'regulates'],
        colors: {
            activates: { core: '#10b981', aura: 'rgba(16, 185, 129, 0.4)' },
            inhibits: { core: '#ef4444', aura: 'rgba(239, 68, 68, 0.4)' },
            binds: { core: '#a78bfa', aura: 'rgba(167, 139, 250, 0.4)' },
            regulates: { core: '#f59e0b', aura: 'rgba(245, 158, 11, 0.4)' }
        }
    };

    // ========================================================================
    // PURE FUNCTIONS - Particle Physics
    // ========================================================================

    /**
     * Create a new particle with random properties
     * @param {Object} bounds - {width, height}
     * @returns {Object} Particle object
     */
    function createParticle(bounds) {
        return {
            x: Math.random() * bounds.width,
            y: Math.random() * bounds.height,
            vx: 0,
            vy: 0,
            size: Math.random() * (CONFIG.maxSize - CONFIG.minSize) + CONFIG.minSize,
            opacity: Math.random() * (CONFIG.maxOpacity - CONFIG.minOpacity) + CONFIG.minOpacity,
            offset: Math.random() * 1000, // For Perlin noise phase
            type: CONFIG.types[Math.floor(Math.random() * CONFIG.types.length)],
            breathePhase: Math.random() * Math.PI * 2 // Random breathing phase
        };
    }

    /**
     * Simplified Perlin noise simulation using sine waves
     * @param {number} x - Input value
     * @returns {number} Noise value between -1 and 1
     */
    function noise(x) {
        // Multi-octave sine wave for organic-looking noise
        const oct1 = Math.sin(x);
        const oct2 = Math.sin(x * 2.34 + 1.2) * 0.5;
        const oct3 = Math.sin(x * 5.71 + 2.8) * 0.25;
        return (oct1 + oct2 + oct3) / 1.75;
    }

    /**
     * Update particle position with organic drift
     * @param {Object} p - Particle
     * @param {number} deltaTime - Time since last frame (ms)
     * @param {Object} bounds - {width, height}
     * @returns {Object} Updated particle
     */
    function updateParticle(p, deltaTime, bounds) {
        const time = Date.now() * 0.0001; // Convert to seconds with scaling

        // Organic drift using Perlin-like noise
        const angleX = (time + p.offset) * Math.PI * 2;
        const angleY = (time + p.offset + 100) * Math.PI * 2;

        p.vx = noise(angleX) * CONFIG.driftSpeed;
        p.vy = noise(angleY) * CONFIG.driftSpeed * 0.7; // Slower vertical drift

        p.x += p.vx;
        p.y += p.vy;

        // Wrap around edges (infinite space)
        if (p.x < -10) p.x = bounds.width + 10;
        if (p.x > bounds.width + 10) p.x = -10;
        if (p.y < -10) p.y = bounds.height + 10;
        if (p.y > bounds.height + 10) p.y = -10;

        // Breathing animation (slow pulse)
        p.breathePhase += deltaTime * 0.001;
        p.currentOpacity = p.opacity + Math.sin(p.breathePhase) * 0.1;
        p.currentOpacity = Math.max(0.1, Math.min(1, p.currentOpacity));

        return p;
    }

    // ========================================================================
    // IMPERATIVE SHELL - DOM & Canvas Rendering
    // ========================================================================

    /**
     * Initialize the particle system
     * @param {string} containerId - ID of container element
     */
    function init(containerId) {
        const container = document.getElementById(containerId);
        if (!container) {
            console.warn('NeuralParticles: Container not found:', containerId);
            return;
        }

        // Create canvas if not exists
        canvas = document.getElementById('neural-particles');
        if (!canvas) {
            console.warn('NeuralParticles: Canvas element not found');
            return;
        }

        ctx = canvas.getContext('2d');
        resize();

        // Create particles
        const bounds = { width: canvas.width, height: canvas.height };
        particles = [];
        for (let i = 0; i < CONFIG.particleCount; i++) {
            particles.push(createParticle(bounds));
        }

        // Start animation
        lastTime = performance.now();
        animate();

        // Handle resize
        window.addEventListener('resize', resize);
    }

    /**
     * Resize canvas to match container
     */
    function resize() {
        if (!canvas) return;

        const rect = canvas.parentElement.getBoundingClientRect();
        canvas.width = rect.width;
        canvas.height = rect.height;
    }

    /**
     * Animation loop (imperative)
     */
    function animate() {
        const currentTime = performance.now();
        const deltaTime = currentTime - lastTime;
        lastTime = currentTime;

        const bounds = { width: canvas.width, height: canvas.height };

        // Clear canvas
        ctx.clearRect(0, 0, bounds.width, bounds.height);

        // Update and render particles
        particles = particles.map(p => updateParticle(p, deltaTime, bounds));

        particles.forEach(p => {
            const colors = CONFIG.colors[p.type];

            // Outer glow (aura)
            const auraRadius = p.size * 6;
            const auraGradient = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, auraRadius);
            auraGradient.addColorStop(0, colors.aura.replace(/[\d.]+\)$/g, (p.currentOpacity * 0.6) + ')'));
            auraGradient.addColorStop(1, 'transparent');

            ctx.fillStyle = auraGradient;
            ctx.beginPath();
            ctx.arc(p.x, p.y, auraRadius, 0, Math.PI * 2);
            ctx.fill();

            // Inner core (bright dot)
            const coreRadius = p.size;
            const coreGradient = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, coreRadius);
            coreGradient.addColorStop(0, colors.core);
            coreGradient.addColorStop(1, colors.aura.replace(/[\d.]+\)$/g, (p.currentOpacity * 0.8) + ')'));

            ctx.fillStyle = coreGradient;
            ctx.globalAlpha = p.currentOpacity;
            ctx.beginPath();
            ctx.arc(p.x, p.y, coreRadius, 0, Math.PI * 2);
            ctx.fill();
            ctx.globalAlpha = 1;
        });

        // Continue animation
        animationId = requestAnimationFrame(animate);
    }

    /**
     * Stop the particle system
     */
    function stop() {
        if (animationId) {
            cancelAnimationFrame(animationId);
            animationId = null;
        }
        window.removeEventListener('resize', resize);
    }

    // ========================================================================
    // PUBLIC API
    // ========================================================================

    return {
        init,
        stop,
        resize
    };
})();

// Auto-initialize when DOM is ready (if card-view exists)
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        if (document.getElementById('card-view')) {
            NeuralParticles.init('card-view');
        }
    });
} else {
    if (document.getElementById('card-view')) {
        NeuralParticles.init('card-view');
    }
}

// Cleanup on page unload
window.addEventListener('beforeunload', () => {
    NeuralParticles.stop();
});
