/**
 * PocketPaw Tools Module
 * Handles tool-specific UI interactions and formatting
 */

const Tools = {
    /**
     * Format message content (markdown-like)
     */
    formatMessage(content) {
        if (!content) return '';

        // Use marked + DOMPurify if available, else regex fallback
        if (typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined') {
            try {
                const renderer = new marked.Renderer();
                // Open links in new tab
                renderer.link = function ({ href, title, text }) {
                    const titleAttr = title ? ` title="${title}"` : '';
                    return `<a href="${href}"${titleAttr} target="_blank" rel="noopener noreferrer">${text}</a>`;
                };
                const html = marked.parse(content, {
                    breaks: true,
                    gfm: true,
                    renderer,
                });
                return DOMPurify.sanitize(html, {
                    ALLOWED_TAGS: [
                        'p', 'br', 'strong', 'em', 'del', 'a', 'code', 'pre',
                        'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                        'blockquote', 'table', 'thead', 'tbody', 'tr', 'th', 'td',
                        'hr', 'img', 'span', 'div', 'sup', 'sub',
                    ],
                    ALLOWED_ATTR: [
                        'href', 'target', 'rel', 'title', 'src', 'alt',
                        'class', 'id',
                    ],
                });
            } catch (_) {
                // Fall through to regex fallback
            }
        }

        // Regex fallback (original behaviour)
        let formatted = content
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');

        formatted = formatted.replace(
            /```(\w*)\n?([\s\S]*?)```/g,
            '<pre><code>$2</code></pre>'
        );
        formatted = formatted.replace(/`([^`]+)`/g, '<code>$1</code>');
        formatted = formatted.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
        formatted = formatted.replace(/\n/g, '<br>');

        return formatted;
    },

    /**
     * Format current time
     */
    formatTime(date = new Date()) {
        return date.toLocaleTimeString('en-US', { 
            hour: '2-digit', 
            minute: '2-digit',
            hour12: true 
        });
    },

    /**
     * Show toast notification
     */
    showToast(message, type = 'info', container) {
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        
        const icons = {
            success: '✅',
            error: '❌',
            info: 'ℹ️',
            warning: '⚠️'
        };
        
        toast.innerHTML = `
            <span class="toast-icon">${icons[type] || icons.info}</span>
            <span class="toast-msg">${message}</span>
        `;
        
        container.appendChild(toast);
        
        // Auto remove after 3s
        setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateX(100%)';
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    },

    /**
     * Parse system status from response
     */
    parseStatus(content) {
        const status = {
            cpu: '—',
            ram: '—',
            disk: '—',
            battery: '—'
        };
        
        if (!content) return status;
        
        // Parse CPU: "🧠 CPU: 50.0% (8 cores)"
        const cpuMatch = content.match(/CPU:\s*([\d.]+)%/);
        if (cpuMatch) status.cpu = Math.round(parseFloat(cpuMatch[1]));
        
        // Parse RAM: "💾 RAM: 10.0 / 16.0 GB (60%)"
        const ramMatch = content.match(/RAM:.*?\(([\d.]+)%\)/);
        if (ramMatch) status.ram = Math.round(parseFloat(ramMatch[1]));
        
        // Parse Disk: "💿 Disk: 200 / 500 GB (40%)"
        const diskMatch = content.match(/Disk:.*?\(([\d.]+)%\)/);
        if (diskMatch) status.disk = Math.round(parseFloat(diskMatch[1]));
        
        // Parse Battery: "🔋 Battery: 80%"
        const batteryMatch = content.match(/Battery:\s*([\d.]+)%/);
        if (batteryMatch) status.battery = Math.round(parseFloat(batteryMatch[1]));
        
        return status;
    },

    /**
     * Check if content is a file browser response
     */
    isFileBrowser(content) {
        return content && content.includes('📁') && content.includes('📂');
    },

    /**
     * Check if content is a screenshot
     */
    isScreenshot(data) {
        return data.type === 'screenshot' && data.image;
    }
};

// Export
window.Tools = Tools;
