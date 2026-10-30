"""Curated style registry for the /studio surface.

Ported from openstory src/lib/style/style-templates.ts (the full seeded
DEFAULT_STYLE_TEMPLATES catalogue). Each style carries its rich visual
config (look + motion + references) so the movie-maker can render a full
detail card, plus a derived ``promptSuffix`` that ``_apply_style`` appends
to the user prompt when the style is "used".

Generated 2026-08-26 (studio-style-registry).
"""

from __future__ import annotations


def _suffix(items: list[tuple[str, str]], references: list[str]) -> str:
    """Compose the concise prompt treatment appended when a style is used."""
    parts = [f"{k}: {v}" for k, v in items if v and str(v).strip()]
    text = ". ".join(parts) + ("." if parts else "")
    if references:
        text += f" Inspired by: {', '.join(references)}."
    return f" {text}" if text.strip() else ""


CURATED_STYLES: list[dict] = [
    {
        "id": "product-ad",
        "label": "Product Ad",
        "description": "Fresh, tactile product content with lifestyle context and sensory detail. Designed for Instagram, DTC brands, e-commerce, and social-first campaigns.",
        "category": "ecommerce",
        "tags": ["ecommerce", "product", "instagram", "dtc", "lifestyle", "social"],
        "promptSuffix": " Art style: Modern social-first product photography with tactile, editorial energy. Products shown in real-life context -- hands opening packaging, fingers pressing textures, products on bathroom shelves, kitchen counters, rumpled linen. Close-up detail shots emphasize material and finish. Flat-lays with curated minimal arrangements. Color-matched backgrounds that complement the product. Every frame feels like something you would screenshot and save. Mood: Fresh, sensory, and effortlessly cool. Lighting: Bright natural window light with clean directional shadows. Direct on-camera flash for punchy editorial energy on select shots. No heavy diffusion -- let light feel real and immediate. Warm late-afternoon glow for lifestyle moments. High-key and airy overall with pops of contrast. Camera: Dynamic mix of handheld and locked shots with consistent energy. Handheld with natural micro-movement for lifestyle moments -- hands interacting, daily rituals, real context. Quick-cut to locked beauty frames for hero product shots. Macro details on textures and surfaces. Overhead flat-lays directly above. Eye-level and slightly above angles. Shallow depth of field on tactile details. Energetic pacing -- no lingering, every frame earns its time. Color grading: Clean and bright with true-to-life color. Whites are crisp, skin tones warm and natural. Minimal processing -- the product looks like it does in your hand. Slight warmth in highlights, lifted shadows keeping everything airy. One accent color pops against neutral base. Inspired by: modern social-first skincare-brand content, dewy minimalist beauty-brand visual identity, sun-soaked lifestyle beauty campaigns, playful color-blocked skincare content.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Fresh, sensory, and effortlessly cool",
                "artStyle": "Modern social-first product photography with tactile, editorial energy. Products shown in real-life context -- hands opening packaging, fingers pressing textures, products on bathroom shelves, kitchen counters, rumpled linen. Close-up detail shots emphasize material and finish. Flat-lays with curated minimal arrangements. Color-matched backgrounds that complement the product. Every frame feels like something you would screenshot and save",
                "lighting": "Bright natural window light with clean directional shadows. Direct on-camera flash for punchy editorial energy on select shots. No heavy diffusion -- let light feel real and immediate. Warm late-afternoon glow for lifestyle moments. High-key and airy overall with pops of contrast",
                "colorGrading": "Clean and bright with true-to-life color. Whites are crisp, skin tones warm and natural. Minimal processing -- the product looks like it does in your hand. Slight warmth in highlights, lifted shadows keeping everything airy. One accent color pops against neutral base",
                "colorPalette": ["#FFFFFF", "#F0E6D3", "#D4536D", "#1A1A1A", "#E8F4E8"],
            },
            "motion": {
                "camera": "Dynamic mix of handheld and locked shots with consistent energy. Handheld with natural micro-movement for lifestyle moments -- hands interacting, daily rituals, real context. Quick-cut to locked beauty frames for hero product shots. Macro details on textures and surfaces. Overhead flat-lays directly above. Eye-level and slightly above angles. Shallow depth of field on tactile details. Energetic pacing -- no lingering, every frame earns its time"
            },
            "references": [
                "modern social-first skincare-brand content",
                "dewy minimalist beauty-brand visual identity",
                "sun-soaked lifestyle beauty campaigns",
                "playful color-blocked skincare content",
            ],
        },
    },
    {
        "id": "real-estate",
        "label": "Real Estate",
        "description": "Prestige property cinematography with warm low-sun glow and aspirational lifestyle framing. Glamorous figures inhabit sun-drenched interiors, adding warmth and scale to luxury spaces. Designed for high-end real estate branding, lifestyle property films, and luxury development showcases.",
        "category": "commercial",
        "tags": ["real-estate", "property", "luxury", "lifestyle", "prestige", "interior-design"],
        "promptSuffix": " Art style: Prestige property cinematography with editorial lifestyle sensibility. Luxury interiors shot with depth and grandeur -- marble surfaces, floor-to-ceiling windows, curated furnishings. Elegant women in designer loungewear or evening attire occupy the spaces naturally -- reading on a linen sofa, pouring wine at a kitchen island, silhouetted against a sunset terrace. The architecture dominates every frame while human presence adds warmth, scale, and aspiration. Compositions emphasize clean sight lines, spatial depth, and the interplay of warm raking light with rich materials. Mood: Luxurious, aspirational, and effortlessly glamorous. Lighting: Late-afternoon sun streaming through expansive windows, casting long warm beams across polished floors and textured surfaces. Rim light catching hair and shoulders of figures in the space. Balanced ambient fill preserving detail in corners and alcoves. Interior spaces glow with warm artificial accents -- table lamps, pendant fixtures -- blending seamlessly with fading daylight. Camera: Slow, cinematic dolly movements through grand interiors at eye level. Smooth reveals through doorways framing figures in the distance. Wide establishing shots of exteriors in warm low-angle sun, intimate medium shots of lifestyle moments. Shallow depth of field isolating textures and details -- a hand on a marble countertop, light catching crystal glassware. Symmetrical compositions for architectural grandeur, rule-of-thirds for lifestyle vignettes. Color grading: Warm and luminous with rich golden highlights and creamy skin tones. Lifted shadows keeping interiors airy and inviting. Subtle amber shift throughout, with deep walnut tones in shadows. Skin rendered with warmth and softness. Overall palette feels like late-afternoon sun on travertine. Inspired by: prestige international realty brand films, impeccably tailored 1960s modernist interiors, old-money corporate-dynasty drama cinematography, opulent jazz-age mansion production design.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Luxurious, aspirational, and effortlessly glamorous",
                "artStyle": "Prestige property cinematography with editorial lifestyle sensibility. Luxury interiors shot with depth and grandeur -- marble surfaces, floor-to-ceiling windows, curated furnishings. Elegant women in designer loungewear or evening attire occupy the spaces naturally -- reading on a linen sofa, pouring wine at a kitchen island, silhouetted against a sunset terrace. The architecture dominates every frame while human presence adds warmth, scale, and aspiration. Compositions emphasize clean sight lines, spatial depth, and the interplay of warm raking light with rich materials",
                "lighting": "Late-afternoon sun streaming through expansive windows, casting long warm beams across polished floors and textured surfaces. Rim light catching hair and shoulders of figures in the space. Balanced ambient fill preserving detail in corners and alcoves. Interior spaces glow with warm artificial accents -- table lamps, pendant fixtures -- blending seamlessly with fading daylight",
                "colorGrading": "Warm and luminous with rich golden highlights and creamy skin tones. Lifted shadows keeping interiors airy and inviting. Subtle amber shift throughout, with deep walnut tones in shadows. Skin rendered with warmth and softness. Overall palette feels like late-afternoon sun on travertine",
                "colorPalette": ["#F5EDE3", "#C9A96E", "#6B4C3B", "#E8D5C4", "#2C2420"],
            },
            "motion": {
                "camera": "Slow, cinematic dolly movements through grand interiors at eye level. Smooth reveals through doorways framing figures in the distance. Wide establishing shots of exteriors in warm low-angle sun, intimate medium shots of lifestyle moments. Shallow depth of field isolating textures and details -- a hand on a marble countertop, light catching crystal glassware. Symmetrical compositions for architectural grandeur, rule-of-thirds for lifestyle vignettes"
            },
            "references": [
                "prestige international realty brand films",
                "impeccably tailored 1960s modernist interiors",
                "old-money corporate-dynasty drama cinematography",
                "opulent jazz-age mansion production design",
            ],
        },
    },
    {
        "id": "animatic",
        "label": "Animatic",
        "description": "Rough storyboard pre-visualization aesthetic with hand-drawn line work and limited shading. Designed for animation pre-production, story reels, pitch boards, and any project that wants a working-draft, sketchpad feel.",
        "category": "animatic",
        "tags": [
            "animatic",
            "storyboard",
            "sketch",
            "pre-vis",
            "rough",
            "hand-drawn",
            "pencil",
            "marker",
        ],
        "promptSuffix": " Art style: A single film frame drawn as rough pre-vis -- pencil-and-marker line work over visible underdrawing, simplified silhouettes, flat blocked-in tones, occasional motion lines and action arrows. Faces and hands described with a few decisive strokes -- expressive but not refined. Minimal background detail, just enough to read the staging. One full-bleed sketched frame depicting the scene -- not a grid of panels, and no frame numbers or margin notes. Explicitly NOT polished animation, NOT 3D rendered, NOT photographic, NOT a finished illustration. This is pre-vis -- the bones of a single shot before it is animated. Mood: Rough, working-draft, evocative but unfinished -- the look of a story being figured out frame by frame, with energy in the gesture rather than polish. Lighting: Implied lighting only. Shape and value suggested through marker shading, hatching, or a single value-block of grey wash. No rendered light, no specular highlights, no atmospheric haze -- the scene reads light and dark, not lit. Camera: Composition staged in 2D like rough pre-vis -- a single clearly readable wide, medium, or close that reads the staging at a glance. Plain, slow camera intent (a gentle push-in or pan) at story-reel pacing, the key beat held. No drawn arrows, nested frames, ghosted move-diagrams, or multiple panels within the image -- one staged frame. Color grading: Paper-tone background -- warm off-white with light grain and the occasional smudge. Marker-bleed warmth in mid-tones, slightly bluish in cool shadows. Reads like a rough hand-sketched frame, not a graded film frame. Inspired by: feature-animation storyboard reels, 90s hand-drawn feature production animatics, studio story reels with gestural pencil energy, painterly hand-drawn feature imageboards.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Rough, working-draft, evocative but unfinished -- the look of a story being figured out frame by frame, with energy in the gesture rather than polish",
                "artStyle": "A single film frame drawn as rough pre-vis -- pencil-and-marker line work over visible underdrawing, simplified silhouettes, flat blocked-in tones, occasional motion lines and action arrows. Faces and hands described with a few decisive strokes -- expressive but not refined. Minimal background detail, just enough to read the staging. One full-bleed sketched frame depicting the scene -- not a grid of panels, and no frame numbers or margin notes. Explicitly NOT polished animation, NOT 3D rendered, NOT photographic, NOT a finished illustration. This is pre-vis -- the bones of a single shot before it is animated",
                "lighting": "Implied lighting only. Shape and value suggested through marker shading, hatching, or a single value-block of grey wash. No rendered light, no specular highlights, no atmospheric haze -- the scene reads light and dark, not lit",
                "colorGrading": "Paper-tone background -- warm off-white with light grain and the occasional smudge. Marker-bleed warmth in mid-tones, slightly bluish in cool shadows. Reads like a rough hand-sketched frame, not a graded film frame",
                "colorPalette": ["#F5F1E8", "#2B2826", "#7A6F66", "#C44536", "#3A6B8C"],
            },
            "motion": {
                "camera": "Composition staged in 2D like rough pre-vis -- a single clearly readable wide, medium, or close that reads the staging at a glance. Plain, slow camera intent (a gentle push-in or pan) at story-reel pacing, the key beat held. No drawn arrows, nested frames, ghosted move-diagrams, or multiple panels within the image -- one staged frame"
            },
            "references": [
                "feature-animation storyboard reels",
                "90s hand-drawn feature production animatics",
                "studio story reels with gestural pencil energy",
                "painterly hand-drawn feature imageboards",
            ],
        },
    },
    {
        "id": "corporate",
        "label": "Corporate",
        "description": "Clean, professional visuals with contemporary design sensibility. Ideal for company culture videos, SaaS product demos, training content, and corporate communications.",
        "category": "tech",
        "tags": ["corporate", "saas", "business", "professional", "training", "tech"],
        "promptSuffix": " Art style: Contemporary corporate visual style with clean geometry and professional environments. Modern office spaces, collaborative workspaces, and technology-forward settings. People appear natural and engaged, not staged. Compositions are balanced and uncluttered with intentional use of negative space. Mood: Professional, innovative, and trustworthy. Lighting: Bright, even overhead lighting typical of modern offices with large windows. Soft and clean with no dramatic shadows. Natural daylight supplemented by warm artificial ambiance. Flattering and professional without being clinical. Camera: Smooth dolly or gimbal movements through workspace environments. Static or slow-push medium shots for interviews and presentations. Over-the-shoulder angles for screen and product demonstrations. Clean, corporate B-roll pacing. Color grading: Clean and modern with slight cool cast. Whites are bright and true, skin tones natural. Subtle blue tint in shadows for a tech-forward feel. Overall bright and airy with controlled, professional color rendering. Inspired by: payments-platform brand films, productivity-software product videos, tech-company culture videos, enterprise-software conference keynotes.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Professional, innovative, and trustworthy",
                "artStyle": "Contemporary corporate visual style with clean geometry and professional environments. Modern office spaces, collaborative workspaces, and technology-forward settings. People appear natural and engaged, not staged. Compositions are balanced and uncluttered with intentional use of negative space",
                "lighting": "Bright, even overhead lighting typical of modern offices with large windows. Soft and clean with no dramatic shadows. Natural daylight supplemented by warm artificial ambiance. Flattering and professional without being clinical",
                "colorGrading": "Clean and modern with slight cool cast. Whites are bright and true, skin tones natural. Subtle blue tint in shadows for a tech-forward feel. Overall bright and airy with controlled, professional color rendering",
                "colorPalette": ["#0066FF", "#F8F9FA", "#1A1A2E", "#00C853", "#6C757D"],
            },
            "motion": {
                "camera": "Smooth dolly or gimbal movements through workspace environments. Static or slow-push medium shots for interviews and presentations. Over-the-shoulder angles for screen and product demonstrations. Clean, corporate B-roll pacing"
            },
            "references": [
                "payments-platform brand films",
                "productivity-software product videos",
                "tech-company culture videos",
                "enterprise-software conference keynotes",
            ],
        },
    },
    {
        "id": "award-season",
        "label": "Award Season",
        "description": "Deep, emotional storytelling with rich cinematography. Perfect for character-driven narratives.",
        "category": "film",
        "tags": ["drama", "emotional", "character-driven", "cinematic"],
        "promptSuffix": " Art style: Cinematic drama with deep shadows and warm tones. Mood: Introspective and emotional. Lighting: Dramatic chiaroscuro lighting with strong contrast. Camera: Slow, deliberate movements with meaningful close-ups. Color grading: Warm highlights with cool shadows. Inspired by: 1970s crime-saga chiaroscuro with amber tungsten interiors, stark oil-frontier epic with sun-scorched landscapes, intimate moonlit coming-of-age drama with luminous skin tones.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Introspective and emotional",
                "artStyle": "Cinematic drama with deep shadows and warm tones",
                "lighting": "Dramatic chiaroscuro lighting with strong contrast",
                "colorGrading": "Warm highlights with cool shadows",
                "colorPalette": ["#8B4513", "#D2691E", "#F4A460", "#2F4F4F", "#708090"],
            },
            "motion": {"camera": "Slow, deliberate movements with meaningful close-ups"},
            "references": [
                "1970s crime-saga chiaroscuro with amber tungsten interiors",
                "stark oil-frontier epic with sun-scorched landscapes",
                "intimate moonlit coming-of-age drama with luminous skin tones",
            ],
        },
    },
    {
        "id": "documentary",
        "label": "Documentary",
        "description": "Natural, observational style with authentic lighting and handheld movement.",
        "category": "film",
        "tags": ["documentary", "realistic", "natural", "authentic", "observational"],
        "promptSuffix": " Art style: Natural documentary style with authentic environments. Mood: Authentic and immediate. Lighting: Natural and available light only. Camera: Handheld camera with observational framing. Color grading: Natural color with slight desaturation. Inspired by: vertiginous big-wall climbing documentary, unflinching observational documentary portraiture, tense vérité whistleblower documentary.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Authentic and immediate",
                "artStyle": "Natural documentary style with authentic environments",
                "lighting": "Natural and available light only",
                "colorGrading": "Natural color with slight desaturation",
                "colorPalette": ["#8B7355", "#CD853F", "#DEB887", "#F5DEB3", "#FFE4B5"],
            },
            "motion": {"camera": "Handheld camera with observational framing"},
            "references": [
                "vertiginous big-wall climbing documentary",
                "unflinching observational documentary portraiture",
                "tense vérité whistleblower documentary",
            ],
        },
    },
    {
        "id": "action",
        "label": "Action",
        "description": "High-energy visuals with dynamic camera work and explosive color palette.",
        "category": "film",
        "tags": ["action", "blockbuster", "explosive", "dynamic", "adventure"],
        "promptSuffix": " Art style: High-octane action with dynamic compositions. Mood: Exciting and adrenaline-pumping. Lighting: High contrast with dramatic rim lighting. Camera: Fast cuts, sweeping crane shots, and dynamic angles. Color grading: Saturated colors with orange and teal contrast. Inspired by: high-octane desert-chase blockbuster, neon-soaked assassin-thriller choreography, globe-trotting spy-caper set pieces.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Exciting and adrenaline-pumping",
                "artStyle": "High-octane action with dynamic compositions",
                "lighting": "High contrast with dramatic rim lighting",
                "colorGrading": "Saturated colors with orange and teal contrast",
                "colorPalette": ["#FF4500", "#FFD700", "#1E90FF", "#FF6347", "#FFA500"],
            },
            "motion": {"camera": "Fast cuts, sweeping crane shots, and dynamic angles"},
            "references": [
                "high-octane desert-chase blockbuster",
                "neon-soaked assassin-thriller choreography",
                "globe-trotting spy-caper set pieces",
            ],
        },
    },
    {
        "id": "rom-com",
        "label": "Rom-Com",
        "description": "Bright, warm visuals with soft lighting and cheerful compositions.",
        "category": "film",
        "tags": ["romance", "comedy", "lighthearted", "warm", "feelgood"],
        "promptSuffix": " Art style: Warm and inviting with soft, romantic lighting. Mood: Light, romantic, and optimistic. Lighting: Soft, diffused lighting with warm tones. Camera: Smooth movements with intimate framing. Color grading: Warm and saturated with soft contrast. Inspired by: technicolor musical romance at magic hour, whimsical Parisian romance with saturated greens and reds, classic autumn-in-the-city romantic comedy.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Light, romantic, and optimistic",
                "artStyle": "Warm and inviting with soft, romantic lighting",
                "lighting": "Soft, diffused lighting with warm tones",
                "colorGrading": "Warm and saturated with soft contrast",
                "colorPalette": ["#FFC0CB", "#FFDAB9", "#FFE4E1", "#F0FFFF", "#FFFACD"],
            },
            "motion": {"camera": "Smooth movements with intimate framing"},
            "references": [
                "technicolor musical romance at magic hour",
                "whimsical Parisian romance with saturated greens and reds",
                "classic autumn-in-the-city romantic comedy",
            ],
        },
    },
    {
        "id": "animated",
        "label": "Animated",
        "description": "Premium, adult-oriented animation with rich textures, painterly detail, and cinematic depth. Built for sophisticated storytelling, dark fantasy, sci-fi, and narrative-driven content.",
        "category": "animation",
        "tags": ["animation", "sophisticated", "cinematic", "dark", "premium", "narrative"],
        "promptSuffix": " Art style: High-fidelity stylized animation with painterly textures and hand-crafted detail. Environments are richly layered with depth and atmosphere -- decayed grandeur, neon-lit cityscapes, or lush otherworldly landscapes. Characters have grounded proportions with expressive, nuanced faces. Every frame composed like a standalone illustration. Mood: Intense, layered, and emotionally complex. Lighting: Dramatic volumetric lighting with god rays, atmospheric haze, and deep contrast. Motivated sources -- firelight, neon signage, bioluminescence -- casting colored shadows. Rim lighting separates characters from richly detailed backgrounds. Chiaroscuro for emotional weight. Camera: Cinematic camera language -- slow tracking shots through detailed environments, dramatic rack focuses between foreground and background layers. Low angles for power, high angles for vulnerability. Long takes that let the world breathe, punctuated by sharp editorial cuts for impact. Color grading: Deep, moody palette with crushed blacks and selective saturation. Warm amber and gold for intimate scenes, cold steel blue for tension. Rich jewel tones used sparingly as accent. Overall filmic with subtle grain and texture overlay. Inspired by: painterly neon-lit prestige fantasy animation, adult sci-fi anthology animation with cinematic render quality, kinetic halftone comic-book animation, hand-drawn folkloric animation with woodcut linework.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Intense, layered, and emotionally complex",
                "artStyle": "High-fidelity stylized animation with painterly textures and hand-crafted detail. Environments are richly layered with depth and atmosphere -- decayed grandeur, neon-lit cityscapes, or lush otherworldly landscapes. Characters have grounded proportions with expressive, nuanced faces. Every frame composed like a standalone illustration",
                "lighting": "Dramatic volumetric lighting with god rays, atmospheric haze, and deep contrast. Motivated sources -- firelight, neon signage, bioluminescence -- casting colored shadows. Rim lighting separates characters from richly detailed backgrounds. Chiaroscuro for emotional weight",
                "colorGrading": "Deep, moody palette with crushed blacks and selective saturation. Warm amber and gold for intimate scenes, cold steel blue for tension. Rich jewel tones used sparingly as accent. Overall filmic with subtle grain and texture overlay",
                "colorPalette": ["#1B1F3B", "#C9A227", "#8B2252", "#2E4045", "#D4A574"],
            },
            "motion": {
                "camera": "Cinematic camera language -- slow tracking shots through detailed environments, dramatic rack focuses between foreground and background layers. Low angles for power, high angles for vulnerability. Long takes that let the world breathe, punctuated by sharp editorial cuts for impact"
            },
            "references": [
                "painterly neon-lit prestige fantasy animation",
                "adult sci-fi anthology animation with cinematic render quality",
                "kinetic halftone comic-book animation",
                "hand-drawn folkloric animation with woodcut linework",
            ],
        },
    },
    {
        "id": "neo-noir-thriller",
        "label": "Neo-Noir Thriller",
        "description": "Dark, stylized visuals with high contrast and urban settings. Ideal for mystery and crime stories.",
        "category": "film",
        "tags": ["noir", "thriller", "urban", "mystery", "crime"],
        "promptSuffix": " Art style: Neo-noir with stark contrasts and neon accents. Mood: Tense and mysterious. Lighting: High contrast with venetian blind shadows and neon highlights. Camera: Dutch angles and voyeuristic framing. Color grading: Desaturated with selective color pops. Inspired by: rain-slicked neon-noir cityscape cinematography, high-contrast graphic-novel monochrome with selective color, synthwave night-drive thriller framing.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Tense and mysterious",
                "artStyle": "Neo-noir with stark contrasts and neon accents",
                "lighting": "High contrast with venetian blind shadows and neon highlights",
                "colorGrading": "Desaturated with selective color pops",
                "colorPalette": ["#000000", "#FF0000", "#00CED1", "#4B0082", "#FF1493"],
            },
            "motion": {"camera": "Dutch angles and voyeuristic framing"},
            "references": [
                "rain-slicked neon-noir cityscape cinematography",
                "high-contrast graphic-novel monochrome with selective color",
                "synthwave night-drive thriller framing",
            ],
        },
    },
    {
        "id": "pastel",
        "label": "Pastel",
        "description": "Obsessively symmetrical live-action cinematography with candy-colored pastels, dollhouse interiors, and deadpan whimsy.",
        "category": "film",
        "tags": ["whimsical", "symmetrical", "pastel", "quirky", "artistic"],
        "promptSuffix": " Art style: Obsessively symmetrical, centered, planimetric frontal framing. Meticulously art-directed interiors with period props, patterned wallpaper, corduroy upholstery, brass fixtures, and leather luggage arranged in dollhouse-like environments. Live-action photographic cinematography — NOT cartoon, NOT illustration, NOT animation. Real actors in real sets. Candy-colored pastels dominate every surface: dusty pinks, powder blues, butter yellows, lavender, mint green. Vintage textures and handcrafted details in every frame. Mood: Whimsical melancholy, deadpan charm, nostalgic precision. Lighting: Soft, perfectly even diffused lighting with minimal shadows. Warm tones reminiscent of 1960s-70s Kodak film stock. Flat illumination that emphasizes set design over dramatic shadow. Camera: Centered framing, tracking shots, and planimetric composition. Color grading: Muted saturated pastels with warm vintage film emulsion. Lifted blacks, soft film grain, slightly faded highlights. Every color feels hand-picked and coordinated. Inspired by: symmetrical pastel grand-hotel caper cinematography, storybook scout-camp adventure with planimetric framing, deadpan family-ensemble portraiture in vintage brownstone interiors.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Whimsical melancholy, deadpan charm, nostalgic precision",
                "artStyle": "Obsessively symmetrical, centered, planimetric frontal framing. Meticulously art-directed interiors with period props, patterned wallpaper, corduroy upholstery, brass fixtures, and leather luggage arranged in dollhouse-like environments. Live-action photographic cinematography — NOT cartoon, NOT illustration, NOT animation. Real actors in real sets. Candy-colored pastels dominate every surface: dusty pinks, powder blues, butter yellows, lavender, mint green. Vintage textures and handcrafted details in every frame",
                "lighting": "Soft, perfectly even diffused lighting with minimal shadows. Warm tones reminiscent of 1960s-70s Kodak film stock. Flat illumination that emphasizes set design over dramatic shadow",
                "colorGrading": "Muted saturated pastels with warm vintage film emulsion. Lifted blacks, soft film grain, slightly faded highlights. Every color feels hand-picked and coordinated",
                "colorPalette": ["#FFB6C1", "#87CEEB", "#F0E68C", "#DDA0DD", "#98FB98"],
            },
            "motion": {"camera": "Centered framing, tracking shots, and planimetric composition"},
            "references": [
                "symmetrical pastel grand-hotel caper cinematography",
                "storybook scout-camp adventure with planimetric framing",
                "deadpan family-ensemble portraiture in vintage brownstone interiors",
            ],
        },
    },
    {
        "id": "sci-fi-futuristic",
        "label": "Sci-Fi Futuristic",
        "description": "Clean, high-tech aesthetics with cool tones and sleek designs.",
        "category": "film",
        "tags": ["scifi", "futuristic", "technology", "space", "cyberpunk"],
        "promptSuffix": " Art style: Futuristic sci-fi with clean lines and holographic elements. Mood: Futuristic and technological. Lighting: Cool LED lighting with lens flares. Camera: Smooth camera movements with wide establishing shots. Color grading: Cool blues and teals with high contrast. Inspired by: minimalist AI-laboratory chamber sci-fi, contemplative first-contact sci-fi with soft atmospheric haze, epic space-time odyssey with practical-scale grandeur.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Futuristic and technological",
                "artStyle": "Futuristic sci-fi with clean lines and holographic elements",
                "lighting": "Cool LED lighting with lens flares",
                "colorGrading": "Cool blues and teals with high contrast",
                "colorPalette": ["#00FFFF", "#0000FF", "#C0C0C0", "#800080", "#00FF00"],
            },
            "motion": {"camera": "Smooth camera movements with wide establishing shots"},
            "references": [
                "minimalist AI-laboratory chamber sci-fi",
                "contemplative first-contact sci-fi with soft atmospheric haze",
                "epic space-time odyssey with practical-scale grandeur",
            ],
        },
    },
    {
        "id": "horror-gothic",
        "label": "Horror Gothic",
        "description": "Dark, atmospheric visuals with Gothic elements and unsettling compositions.",
        "category": "film",
        "tags": ["horror", "gothic", "dark", "atmospheric", "supernatural"],
        "promptSuffix": " Art style: Gothic horror with dark shadows and eerie atmosphere. Mood: Ominous and foreboding. Lighting: Low-key lighting with harsh shadows. Camera: Unsettling angles and slow zooms. Color grading: Desaturated with crushed blacks. Inspired by: candle-lit puritan folk-horror naturalism, slow-burn domestic dread with dollhouse framing, storm-lashed monochrome maritime madness.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Ominous and foreboding",
                "artStyle": "Gothic horror with dark shadows and eerie atmosphere",
                "lighting": "Low-key lighting with harsh shadows",
                "colorGrading": "Desaturated with crushed blacks",
                "colorPalette": ["#1C1C1C", "#8B0000", "#483D8B", "#2F4F4F", "#696969"],
            },
            "motion": {"camera": "Unsettling angles and slow zooms"},
            "references": [
                "candle-lit puritan folk-horror naturalism",
                "slow-burn domestic dread with dollhouse framing",
                "storm-lashed monochrome maritime madness",
            ],
        },
    },
    {
        "id": "western-epic",
        "label": "Western Epic",
        "description": "Wide vistas with dusty, low-sun raking light and classic Western aesthetics.",
        "category": "film",
        "tags": ["western", "epic", "frontier", "classic", "americana"],
        "promptSuffix": " Art style: Classic Western with wide landscapes and raking sunset light. Mood: Epic and frontier-inspired. Lighting: Magic hour lighting with long shadows. Camera: Wide shots, slow zooms, and classic Western framing. Color grading: Warm, dusty tones with high contrast. Inspired by: sun-bleached spaghetti-western standoffs with extreme close-ups, operatic frontier epic with patient widescreen staging, classic desert-valley western vistas framed through doorways.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Epic and frontier-inspired",
                "artStyle": "Classic Western with wide landscapes and raking sunset light",
                "lighting": "Magic hour lighting with long shadows",
                "colorGrading": "Warm, dusty tones with high contrast",
                "colorPalette": ["#D2691E", "#8B4513", "#DEB887", "#CD853F", "#F4A460"],
            },
            "motion": {"camera": "Wide shots, slow zooms, and classic Western framing"},
            "references": [
                "sun-bleached spaghetti-western standoffs with extreme close-ups",
                "operatic frontier epic with patient widescreen staging",
                "classic desert-valley western vistas framed through doorways",
            ],
        },
    },
    {
        "id": "lo-fi-retro",
        "label": "Lo-Fi Retro",
        "description": "Simulates the look of circa-2016 smartphone photography. Characterized by lower resolution, poor dynamic range, digital noise, and crunchy JPEG processing.",
        "category": "film",
        "tags": ["lo-fi", "retro", "amateur", "2010s", "no-text", "digital-noise"],
        "promptSuffix": ' Art style: Retro smartphone JPEG aesthetic. Clean image with absolutely NO text overlays, NO datestamps, and NO time indicators burnt into the visual. Visible digital compression artifacts and over-sharpening. Textures are slightly soft/muddy. Includes sensor limitations: significant digital noise in shadows and color fringing.. Mood: Nostalgic, amateur, authentic snapshot quality with no professional polish. Pure photographic capture.. Lighting: Low dynamic range (LDR). Highlights are blown out/clipped (loss of detail in bright areas like skies or lamps). Shadows are crushed and grainy. Simulates the struggle of older sensors to balance exposure.. Camera: Handheld amateur perspective, f/1.8 aperture. Less sophisticated stabilization implies slight micro-jitters. Focus is decent but not clinical; background separation is digital and less smooth than modern sensors.. Color grading: Standard Rec.709 sRGB with older auto-white balance tendencies (often slightly too cool or too warm). No Log profile. Colors appear "baked in" and digital.. Inspired by: Amateur Vlogs circa 2016, Early Instagram Aesthetic, Raw Phone Camera Roll.',
        "config": {
            "version": 2,
            "look": {
                "mood": "Nostalgic, amateur, authentic snapshot quality with no professional polish. Pure photographic capture.",
                "artStyle": "Retro smartphone JPEG aesthetic. Clean image with absolutely NO text overlays, NO datestamps, and NO time indicators burnt into the visual. Visible digital compression artifacts and over-sharpening. Textures are slightly soft/muddy. Includes sensor limitations: significant digital noise in shadows and color fringing.",
                "lighting": "Low dynamic range (LDR). Highlights are blown out/clipped (loss of detail in bright areas like skies or lamps). Shadows are crushed and grainy. Simulates the struggle of older sensors to balance exposure.",
                "colorGrading": 'Standard Rec.709 sRGB with older auto-white balance tendencies (often slightly too cool or too warm). No Log profile. Colors appear "baked in" and digital.',
                "colorPalette": ["#F5F5DC", "#D2B48C", "#8B4513", "#FFFAF0", "#2F4F4F"],
            },
            "motion": {
                "camera": "Handheld amateur perspective, f/1.8 aperture. Less sophisticated stabilization implies slight micro-jitters. Focus is decent but not clinical; background separation is digital and less smooth than modern sensors."
            },
            "references": [
                "Amateur Vlogs circa 2016",
                "Early Instagram Aesthetic",
                "Raw Phone Camera Roll",
            ],
        },
    },
    {
        "id": "glossy-product-hero",
        "label": "Glossy Product Hero",
        "description": "Polished, ad-agency hero shots with deep blacks, controlled reflections, and a single confident product silhouette. Built for launch films, brand homepages, and digital OOH.",
        "category": "commercial",
        "tags": ["commercial", "hero", "product", "glossy", "brand", "launch"],
        "promptSuffix": " Art style: Studio hero photography with a single product as protagonist. Sculpted gradient backdrops, immaculate surfaces, perfect symmetry. The product is rendered like a sculpture -- every facet, edge, and material highlight intentional. No clutter, no distractions. Just light, surface, and form. Mood: Confident, premium, hyper-controlled. Lighting: Studio strobe and softbox setup. Crisp specular highlights, controlled reflections on glass and metal, deep velvet blacks. Rim light separates product from background. Hero kicker for the brand mark. Camera: Locked-off hero frames, slow rotating turntable moves, hyper-smooth dolly-ins from wide to macro. Beauty pass with shallow depth. No handheld energy -- everything feels designed. Color grading: Rich and contrasty with true blacks and neutral whites. Slight warm accent on metallic highlights. Skin and surface tones rendered faithfully. Print-campaign polish. Inspired by: flagship consumer-electronics product films, high-end audio hardware launches, precision camera-brand heritage films, engineering-led appliance cinema spots.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Confident, premium, hyper-controlled",
                "artStyle": "Studio hero photography with a single product as protagonist. Sculpted gradient backdrops, immaculate surfaces, perfect symmetry. The product is rendered like a sculpture -- every facet, edge, and material highlight intentional. No clutter, no distractions. Just light, surface, and form",
                "lighting": "Studio strobe and softbox setup. Crisp specular highlights, controlled reflections on glass and metal, deep velvet blacks. Rim light separates product from background. Hero kicker for the brand mark",
                "colorGrading": "Rich and contrasty with true blacks and neutral whites. Slight warm accent on metallic highlights. Skin and surface tones rendered faithfully. Print-campaign polish",
                "colorPalette": ["#0A0A0A", "#1F1F1F", "#F2F2F2", "#C8A464", "#FFFFFF"],
            },
            "motion": {
                "camera": "Locked-off hero frames, slow rotating turntable moves, hyper-smooth dolly-ins from wide to macro. Beauty pass with shallow depth. No handheld energy -- everything feels designed"
            },
            "references": [
                "flagship consumer-electronics product films",
                "high-end audio hardware launches",
                "precision camera-brand heritage films",
                "engineering-led appliance cinema spots",
            ],
        },
    },
    {
        "id": "premium-lifestyle",
        "label": "Premium Lifestyle",
        "description": "Aspirational brand-lifestyle storytelling. Beautiful people, beautiful places, the product woven naturally into rituals. Built for luxury brand films and seasonal campaigns.",
        "category": "commercial",
        "tags": ["commercial", "lifestyle", "brand", "premium", "aspirational"],
        "promptSuffix": " Art style: Editorial lifestyle photography with cinematic depth. Model-grade talent in considered wardrobe inhabiting beautiful environments -- linen sofas, sunlit balconies, marble kitchens. The product appears in real ritual moments rather than being presented. Compositions feel composed but unforced. Mood: Elegant, sun-soaked, quietly confident. Lighting: Wraparound natural light supplemented by soft bounce. Window light dominant, warm practicals at night. Warm late-day sun favored for exteriors. Highlights gently rolled off, never clipped. Camera: Slow handheld and tracked moves. Frequent shallow depth, often racking focus from hand to face. Wider clean masters for context, intimate close-ups on touch and texture. Color grading: Warm, low-contrast filmic with creamy highlights. Skin tones soft and natural. Subtle desaturation in shadow, slight green-cream cast overall. Looks like an editorial spread. Inspired by: minimalist apothecary brand films, quietly surreal luxury fashion-house campaigns, understated minimalist fashion lookbooks, serene luxury resort films.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Elegant, sun-soaked, quietly confident",
                "artStyle": "Editorial lifestyle photography with cinematic depth. Model-grade talent in considered wardrobe inhabiting beautiful environments -- linen sofas, sunlit balconies, marble kitchens. The product appears in real ritual moments rather than being presented. Compositions feel composed but unforced",
                "lighting": "Wraparound natural light supplemented by soft bounce. Window light dominant, warm practicals at night. Warm late-day sun favored for exteriors. Highlights gently rolled off, never clipped",
                "colorGrading": "Warm, low-contrast filmic with creamy highlights. Skin tones soft and natural. Subtle desaturation in shadow, slight green-cream cast overall. Looks like an editorial spread",
                "colorPalette": ["#F4EDE2", "#C9A682", "#3B2F26", "#8AA39B", "#FFFFFF"],
            },
            "motion": {
                "camera": "Slow handheld and tracked moves. Frequent shallow depth, often racking focus from hand to face. Wider clean masters for context, intimate close-ups on touch and texture"
            },
            "references": [
                "minimalist apothecary brand films",
                "quietly surreal luxury fashion-house campaigns",
                "understated minimalist fashion lookbooks",
                "serene luxury resort films",
            ],
        },
    },
    {
        "id": "beach-ritual",
        "label": "Beach Ritual",
        "description": "Sun-soaked beachside lifestyle storytelling — beautiful people, golden light, and the product woven into easy coastal rituals. Built for summer, swimwear, beauty, and beverage campaigns.",
        "category": "commercial",
        "tags": ["commercial", "lifestyle", "beach", "summer", "aspirational", "swimwear"],
        "promptSuffix": " Art style: Editorial coastal lifestyle. Model-grade talent on sand, surf, and weathered timber, the product woven into real beach rituals — sunscreen smoothed on, a cold drink lifted, a towel shaken out, hair pushed back wet from the water. Salt air, golden skin, swimwear and light linen. Compositions feel candid and lived-in, never posed. Mood: Sun-drenched, breezy, effortlessly carefree. Lighting: Hard golden-hour sun and bright coastal glare, natural lens flares welcomed. Warm bounce off sand, sparkling specular highlights on water. Highlights gently rolled off, deep warm shadows. Camera: Handheld and gimbal moves that follow motion — walking out of the surf, a slow track along the shoreline, a whip-pan to a splash. Shallow depth on skin and product, wide masters for the horizon. Breezy, energetic pacing. Color grading: Warm, vibrant filmic — turquoise water, golden skin, creamy highlights. Saturated but natural, a gentle teal-and-tan cast. Looks like a summer cover shoot. Inspired by: sun-soaked summer swimwear campaigns, coastal beauty-brand films, premium beverage beach spots, golden-hour surf lifestyle reels.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Sun-drenched, breezy, effortlessly carefree",
                "artStyle": "Editorial coastal lifestyle. Model-grade talent on sand, surf, and weathered timber, the product woven into real beach rituals — sunscreen smoothed on, a cold drink lifted, a towel shaken out, hair pushed back wet from the water. Salt air, golden skin, swimwear and light linen. Compositions feel candid and lived-in, never posed",
                "lighting": "Hard golden-hour sun and bright coastal glare, natural lens flares welcomed. Warm bounce off sand, sparkling specular highlights on water. Highlights gently rolled off, deep warm shadows",
                "colorGrading": "Warm, vibrant filmic — turquoise water, golden skin, creamy highlights. Saturated but natural, a gentle teal-and-tan cast. Looks like a summer cover shoot",
                "colorPalette": ["#F2D9A0", "#3FA7C4", "#E9E2D0", "#C46B3F", "#FFFFFF"],
            },
            "motion": {
                "camera": "Handheld and gimbal moves that follow motion — walking out of the surf, a slow track along the shoreline, a whip-pan to a splash. Shallow depth on skin and product, wide masters for the horizon. Breezy, energetic pacing"
            },
            "references": [
                "sun-soaked summer swimwear campaigns",
                "coastal beauty-brand films",
                "premium beverage beach spots",
                "golden-hour surf lifestyle reels",
            ],
        },
    },
    {
        "id": "beauty-macro",
        "label": "Beauty Macro",
        "description": "Beauty-counter close-ups with dewy texture, micro-detail, and slow-motion product engagement. Built for skincare, cosmetics, and fragrance launch content.",
        "category": "commercial",
        "tags": ["commercial", "beauty", "macro", "skincare", "cosmetics"],
        "promptSuffix": " Art style: Macro beauty photography focused on texture and material. Cream swirling on porcelain, serum dripping down a glass dropper, lipstick gliding across a fingertip. Skin rendered with realistic pore detail. Backgrounds are simple gradient washes that flatter the product. Mood: Sensory, intimate, luxurious. Lighting: Soft, glowing key with a discreet edge light. Slight backlight to make liquids and gels glow. Even, flattering, never harsh. Subtle bounce into shadows so detail is preserved. Camera: Slow macro push-ins, slight rotational moves, slow-motion liquid pours and ribbon shots. Locked frames for hero product. Probe lens style for unusual angles into bottles and jars. Color grading: Warm and luminous. Highlights softly bloomed, skin tones flushed and dewy. Pinks and peaches gently amplified. Looks like a high-end beauty editorial. Inspired by: glamour cosmetics launch films, luxury skincare brand spots, dewy minimalist beauty films, maximalist editorial makeup launch reels.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Sensory, intimate, luxurious",
                "artStyle": "Macro beauty photography focused on texture and material. Cream swirling on porcelain, serum dripping down a glass dropper, lipstick gliding across a fingertip. Skin rendered with realistic pore detail. Backgrounds are simple gradient washes that flatter the product",
                "lighting": "Soft, glowing key with a discreet edge light. Slight backlight to make liquids and gels glow. Even, flattering, never harsh. Subtle bounce into shadows so detail is preserved",
                "colorGrading": "Warm and luminous. Highlights softly bloomed, skin tones flushed and dewy. Pinks and peaches gently amplified. Looks like a high-end beauty editorial",
                "colorPalette": ["#F8E9DA", "#E2A8A1", "#3D2A24", "#F4D5C5", "#FFFFFF"],
            },
            "motion": {
                "camera": "Slow macro push-ins, slight rotational moves, slow-motion liquid pours and ribbon shots. Locked frames for hero product. Probe lens style for unusual angles into bottles and jars"
            },
            "references": [
                "glamour cosmetics launch films",
                "luxury skincare brand spots",
                "dewy minimalist beauty films",
                "maximalist editorial makeup launch reels",
            ],
        },
    },
    {
        "id": "automotive-cinematic",
        "label": "Automotive Cinematic",
        "description": "Wide cinematic driving content with engineered motion, controlled reflections, and big-sky drama. Built for car brand films and launch teasers.",
        "category": "commercial",
        "tags": ["commercial", "automotive", "cinematic", "driving", "launch"],
        "promptSuffix": " Art style: Cinematic automotive photography on empty roads, salt flats, mountain passes, and reflective urban environments. The car is shot like an architectural subject -- low angles emphasizing stance, high angles revealing line and form. Backgrounds chosen for graphic clarity, never cluttered. Mood: Powerful, engineered, kinetic. Lighting: Low raking sun bleeding into blue hour. Hard light skating across painted surfaces, controlled lens flares. Night content lit by city neon, headlights, and discreet rim sources. Sky and reflections are characters of their own. Camera: Tracking vehicle shots from gimbal cars and aerial drones. Long lens compression for highway sequences, wide for landscape integration. Slow rotational beauty passes around static heroes. Pans that follow the car cleanly through frame. Color grading: Cinematic teal-and-amber with deep saturated paint colors. Skies pushed toward blue, asphalt cool, sunlit faces warm. Crushed shadows, controlled highlights. Looks like a feature film trailer. Inspired by: heritage sports-car brand films, performance-division automotive films, minimalist electric-vehicle launch spots, rugged off-road truck cinema.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Powerful, engineered, kinetic",
                "artStyle": "Cinematic automotive photography on empty roads, salt flats, mountain passes, and reflective urban environments. The car is shot like an architectural subject -- low angles emphasizing stance, high angles revealing line and form. Backgrounds chosen for graphic clarity, never cluttered",
                "lighting": "Low raking sun bleeding into blue hour. Hard light skating across painted surfaces, controlled lens flares. Night content lit by city neon, headlights, and discreet rim sources. Sky and reflections are characters of their own",
                "colorGrading": "Cinematic teal-and-amber with deep saturated paint colors. Skies pushed toward blue, asphalt cool, sunlit faces warm. Crushed shadows, controlled highlights. Looks like a feature film trailer",
                "colorPalette": ["#0F1620", "#1F3A5F", "#C0392B", "#E8E8E8", "#F3C969"],
            },
            "motion": {
                "camera": "Tracking vehicle shots from gimbal cars and aerial drones. Long lens compression for highway sequences, wide for landscape integration. Slow rotational beauty passes around static heroes. Pans that follow the car cleanly through frame"
            },
            "references": [
                "heritage sports-car brand films",
                "performance-division automotive films",
                "minimalist electric-vehicle launch spots",
                "rugged off-road truck cinema",
            ],
        },
    },
    {
        "id": "fashion-editorial",
        "label": "Fashion Editorial",
        "description": "High-fashion editorial cinematography with strong poses, considered locations, and an unmistakable magazine feel. Built for seasonal campaigns and runway films.",
        "category": "commercial",
        "tags": ["commercial", "fashion", "editorial", "runway", "campaign"],
        "promptSuffix": " Art style: Editorial fashion photography brought to motion. Model occupies space with intent -- a single beat held, a slow head turn, a hand brushing fabric. Locations have graphic clarity: bare concrete, painted walls, dunes, raw industrial interiors. Clothing is the protagonist, body the canvas. Mood: Cool, sculptural, deliberate. Lighting: High-contrast hard light from a single direction, or perfectly soft north-window flat light. Shadows are intentional and shaped. No spill, no clutter. Often features one heavy directional source. Camera: Locked or slow-tracked frames at portrait, wide, and detail focal lengths. Model often centered or held to a strong vertical. Slow reveals via dolly-in or pan. Beats held longer than commercial pacing. Color grading: Filmic with deep blacks and slightly desaturated mid-tones. Skin tones cooled fractionally. Strong color accents from wardrobe punch against muted environments. Magazine-print finish. Inspired by: rock-edged monochrome fashion-house campaigns, sculptural leather-goods lookbooks, austere minimalist fashion brand films, sun-drenched provincial runway show films.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Cool, sculptural, deliberate",
                "artStyle": "Editorial fashion photography brought to motion. Model occupies space with intent -- a single beat held, a slow head turn, a hand brushing fabric. Locations have graphic clarity: bare concrete, painted walls, dunes, raw industrial interiors. Clothing is the protagonist, body the canvas",
                "lighting": "High-contrast hard light from a single direction, or perfectly soft north-window flat light. Shadows are intentional and shaped. No spill, no clutter. Often features one heavy directional source",
                "colorGrading": "Filmic with deep blacks and slightly desaturated mid-tones. Skin tones cooled fractionally. Strong color accents from wardrobe punch against muted environments. Magazine-print finish",
                "colorPalette": ["#1A1A1A", "#F0EDE6", "#7F1D1D", "#3F3F3F", "#C2B280"],
            },
            "motion": {
                "camera": "Locked or slow-tracked frames at portrait, wide, and detail focal lengths. Model often centered or held to a strong vertical. Slow reveals via dolly-in or pan. Beats held longer than commercial pacing"
            },
            "references": [
                "rock-edged monochrome fashion-house campaigns",
                "sculptural leather-goods lookbooks",
                "austere minimalist fashion brand films",
                "sun-drenched provincial runway show films",
            ],
        },
    },
    {
        "id": "luxury-still",
        "label": "Luxury Still",
        "description": "Slow, painterly still-life cinematography for premium goods -- spirits, leather goods, ceramics, art objects. Built for boutique brand films and limited-edition launches.",
        "category": "commercial",
        "tags": ["commercial", "luxury", "still-life", "boutique", "craft"],
        "promptSuffix": " Art style: Still-life composition in the tradition of Dutch master painting. A single hero object on a sculpted surface, perhaps with one accompanying prop. Materials emphasized: hand-stitched leather, hand-thrown ceramic, blown glass. Negative space and asymmetry favored over symmetry. Mood: Quiet, reverent, painterly. Lighting: A single soft window-like key from one side, deep ambient shadow on the other. Chiaroscuro in the manner of Dutch master painting. Highlights gentle, falloff dramatic. Surfaces breathe. Camera: Locked frames, glacial pushes, slow orbital tracks. Compositions held in stillness for long enough that the viewer notices the silence. Macro detail passes for texture. Color grading: Rich, oil-painting palette. Deep walnut shadows, golden mid-tones, soft highlights. Subtle warmth across the frame. Looks like a museum-lit object. Inspired by: heritage leather-house brand films, Roman high-jewellery heritage spots, Single Malt Whisky Launch Films, artisan craft-workshop brand films.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Quiet, reverent, painterly",
                "artStyle": "Still-life composition in the tradition of Dutch master painting. A single hero object on a sculpted surface, perhaps with one accompanying prop. Materials emphasized: hand-stitched leather, hand-thrown ceramic, blown glass. Negative space and asymmetry favored over symmetry",
                "lighting": "A single soft window-like key from one side, deep ambient shadow on the other. Chiaroscuro in the manner of Dutch master painting. Highlights gentle, falloff dramatic. Surfaces breathe",
                "colorGrading": "Rich, oil-painting palette. Deep walnut shadows, golden mid-tones, soft highlights. Subtle warmth across the frame. Looks like a museum-lit object",
                "colorPalette": ["#2A2118", "#7A5A3A", "#D6BE94", "#1A1A1A", "#F0E8D6"],
            },
            "motion": {
                "camera": "Locked frames, glacial pushes, slow orbital tracks. Compositions held in stillness for long enough that the viewer notices the silence. Macro detail passes for texture"
            },
            "references": [
                "heritage leather-house brand films",
                "Roman high-jewellery heritage spots",
                "Single Malt Whisky Launch Films",
                "artisan craft-workshop brand films",
            ],
        },
    },
    {
        "id": "food-beverage-hero",
        "label": "Food & Beverage Hero",
        "description": "Mouthwatering food cinematography with controlled steam, slow pours, and sensory micro-detail. Built for restaurant brand films, CPG launches, and recipe content.",
        "category": "commercial",
        "tags": ["commercial", "food", "beverage", "restaurant", "cpg"],
        "promptSuffix": " Art style: Hero food cinematography. Cuts hitting boards with audible weight, butter melting, sauces pulled across plates with the back of a spoon, ice cracking into a glass, steam rising from a fresh espresso. Plating considered, props natural, surfaces honest -- aged wood, marble, raw linen. Mood: Sensory, appetizing, crafted. Lighting: Single soft window-like key from the side or behind. Strong backlight to define steam, sauce sheen, and glassware. Subtle fill, never flat. Highlights pop on garnish. Camera: Macro slow-motion pours, slices, and drips. Overhead flat-lay reveals. Slow tabletop tracking past plated dishes. Probe lens dives into bubbles, pots, and grills. Beats held long enough to feel the texture. Color grading: Warm and rich. Golden highlights, deep saucy shadows, vivid greens for herbs and produce. Slight contrast lift in midtones. Looks delicious before the cut even lands. Inspired by: enamel-cookware brand films, editorial test-kitchen video series, sensory food-science documentary series, nostalgic neighbourhood-bakery spots.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Sensory, appetizing, crafted",
                "artStyle": "Hero food cinematography. Cuts hitting boards with audible weight, butter melting, sauces pulled across plates with the back of a spoon, ice cracking into a glass, steam rising from a fresh espresso. Plating considered, props natural, surfaces honest -- aged wood, marble, raw linen",
                "lighting": "Single soft window-like key from the side or behind. Strong backlight to define steam, sauce sheen, and glassware. Subtle fill, never flat. Highlights pop on garnish",
                "colorGrading": "Warm and rich. Golden highlights, deep saucy shadows, vivid greens for herbs and produce. Slight contrast lift in midtones. Looks delicious before the cut even lands",
                "colorPalette": ["#3F2A1A", "#C97B4E", "#F0D7A1", "#1F2F1F", "#FFFFFF"],
            },
            "motion": {
                "camera": "Macro slow-motion pours, slices, and drips. Overhead flat-lay reveals. Slow tabletop tracking past plated dishes. Probe lens dives into bubbles, pots, and grills. Beats held long enough to feel the texture"
            },
            "references": [
                "enamel-cookware brand films",
                "editorial test-kitchen video series",
                "sensory food-science documentary series",
                "nostalgic neighbourhood-bakery spots",
            ],
        },
    },
    {
        "id": "tech-keynote",
        "label": "Tech Keynote",
        "description": "Clean, stage-ready technology presentation aesthetic with controlled gradients, sharp typography surfaces, and architectural precision. Built for product launches and conference reveals.",
        "category": "tech",
        "tags": ["commercial", "tech", "keynote", "launch", "conference"],
        "promptSuffix": " Art style: Stage-presentation aesthetic. Dark seamless backgrounds with one or two glowing gradient orbs, hero product lit clinically center-frame. UI fragments float in clean perspective. Architectural lines, perfect symmetry, ample negative space. Every element feels engineered. Mood: Precise, future-facing, confident. Lighting: Controlled studio lighting with cool key from above, warmer rim from behind. Black backgrounds illuminated by gradient glow rather than direct light. Product surfaces show clean specular highlights without reflection clutter. Camera: Locked hero frames, slow dolly-ins, perfect rotational reveals. UI flythroughs with measured parallax. No handheld energy, no wobble. Everything moves on rails. Color grading: Cool, contrasty, slightly desaturated. Deep blacks, neon-glass blues, crisp whites. Skin tones pulled fractionally cool. Looks like a stage feed. Inspired by: flagship tech keynote reveal films, electric-vehicle stage launch events, transparent-hardware design-brand films, challenger smartphone hero spots.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Precise, future-facing, confident",
                "artStyle": "Stage-presentation aesthetic. Dark seamless backgrounds with one or two glowing gradient orbs, hero product lit clinically center-frame. UI fragments float in clean perspective. Architectural lines, perfect symmetry, ample negative space. Every element feels engineered",
                "lighting": "Controlled studio lighting with cool key from above, warmer rim from behind. Black backgrounds illuminated by gradient glow rather than direct light. Product surfaces show clean specular highlights without reflection clutter",
                "colorGrading": "Cool, contrasty, slightly desaturated. Deep blacks, neon-glass blues, crisp whites. Skin tones pulled fractionally cool. Looks like a stage feed",
                "colorPalette": ["#0A0A0F", "#1E2A3A", "#6FC2FF", "#FFFFFF", "#A8AFB8"],
            },
            "motion": {
                "camera": "Locked hero frames, slow dolly-ins, perfect rotational reveals. UI flythroughs with measured parallax. No handheld energy, no wobble. Everything moves on rails"
            },
            "references": [
                "flagship tech keynote reveal films",
                "electric-vehicle stage launch events",
                "transparent-hardware design-brand films",
                "challenger smartphone hero spots",
            ],
        },
    },
    {
        "id": "fintech-explainer",
        "label": "Fintech Explainer",
        "description": "Trustworthy, motion-graphics-friendly fintech aesthetic. Clean dashboards, soft UI cards, and human moments of relief and clarity. Built for product explainer films and onboarding.",
        "category": "tech",
        "tags": ["commercial", "fintech", "explainer", "onboarding", "saas"],
        "promptSuffix": " Art style: Clean fintech storytelling that intercuts diverse, relaxed human moments with hyper-readable UI inserts. Apartments lit with warm afternoon light, phones held naturally, dashboards rendered in soft glass-card UI. Money is implied rather than shown -- the focus is on confidence and ease. Mood: Calm, trustworthy, modern. Lighting: Soft natural daylight indoors, gentle key with bounce fill for faces. UI surfaces have subtle inner glow and ambient occlusion. Never harsh, never clinical. Camera: Locked or gently floated handheld for human moments. UI inserts are flat-on with measured push-ins to specific dashboard elements. Smooth transitions from person to screen and back. Color grading: Bright and airy with one brand accent. Whites kept clean, mid-tones gently warm, shadows soft. UI rendered with high fidelity and no banding. Inspired by: borderless-money fintech brand films, neobank onboarding films, fintech super-app product spots, payments-infrastructure explainers.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Calm, trustworthy, modern",
                "artStyle": "Clean fintech storytelling that intercuts diverse, relaxed human moments with hyper-readable UI inserts. Apartments lit with warm afternoon light, phones held naturally, dashboards rendered in soft glass-card UI. Money is implied rather than shown -- the focus is on confidence and ease",
                "lighting": "Soft natural daylight indoors, gentle key with bounce fill for faces. UI surfaces have subtle inner glow and ambient occlusion. Never harsh, never clinical",
                "colorGrading": "Bright and airy with one brand accent. Whites kept clean, mid-tones gently warm, shadows soft. UI rendered with high fidelity and no banding",
                "colorPalette": ["#F7F8FB", "#0A2540", "#22C55E", "#A8B0BC", "#FF7B5A"],
            },
            "motion": {
                "camera": "Locked or gently floated handheld for human moments. UI inserts are flat-on with measured push-ins to specific dashboard elements. Smooth transitions from person to screen and back"
            },
            "references": [
                "borderless-money fintech brand films",
                "neobank onboarding films",
                "fintech super-app product spots",
                "payments-infrastructure explainers",
            ],
        },
    },
    {
        "id": "perfume-editorial",
        "label": "Perfume Editorial",
        "description": "Atmospheric, scent-as-mood perfume cinematography. Bottles in mist, fabric drifting, silhouettes against light. Built for fragrance launch films and seasonal campaigns.",
        "category": "commercial",
        "tags": ["commercial", "perfume", "fragrance", "editorial", "campaign"],
        "promptSuffix": " Art style: Fragrance cinematography that treats scent as cinema. The bottle is one of several subjects: silk drifting in slow motion, a hand brushing a collarbone, a figure backlit through a sheer curtain, a window full of dust catching sun. Locations are spare and architectural. Mood: Mysterious, sensorial, romantic. Lighting: Heavy backlight cutting through atmosphere -- haze, mist, or fine dust. Bottles caught with rim light, faces partial-silhouette. Single hard sun or a single soft window source, never both. Dramatic falloff. Camera: Slow tracking, slow rotation, slow reveal. Soft slow-motion fabric and liquid passes. Beats held in stillness until they almost break. Macro detail on glass, skin, light. Color grading: Warm amber and deep walnut shadows. Skin tones rich, slightly bronzed. Highlights bloomed gently. Looks like film stock from the mid-90s. Inspired by: haute-couture fragrance films, smoldering designer perfume spots, desert-at-dusk cologne campaign films, minimalist niche-fragrance brand films.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Mysterious, sensorial, romantic",
                "artStyle": "Fragrance cinematography that treats scent as cinema. The bottle is one of several subjects: silk drifting in slow motion, a hand brushing a collarbone, a figure backlit through a sheer curtain, a window full of dust catching sun. Locations are spare and architectural",
                "lighting": "Heavy backlight cutting through atmosphere -- haze, mist, or fine dust. Bottles caught with rim light, faces partial-silhouette. Single hard sun or a single soft window source, never both. Dramatic falloff",
                "colorGrading": "Warm amber and deep walnut shadows. Skin tones rich, slightly bronzed. Highlights bloomed gently. Looks like film stock from the mid-90s",
                "colorPalette": ["#1A1410", "#7F3F2C", "#E6D5BD", "#9B6A45", "#FFFFFF"],
            },
            "motion": {
                "camera": "Slow tracking, slow rotation, slow reveal. Soft slow-motion fabric and liquid passes. Beats held in stillness until they almost break. Macro detail on glass, skin, light"
            },
            "references": [
                "haute-couture fragrance films",
                "smoldering designer perfume spots",
                "desert-at-dusk cologne campaign films",
                "minimalist niche-fragrance brand films",
            ],
        },
    },
    {
        "id": "watch-macro",
        "label": "Watch Macro",
        "description": "Hyper-detailed horological cinematography with engineered light catching every facet of case and dial. Built for watch brands, jewellery, and collector launches.",
        "category": "commercial",
        "tags": ["commercial", "watch", "horology", "macro", "luxury"],
        "promptSuffix": " Art style: Macro horology photography. Watch case shown against a deep gradient, dial detail isolated, crown and lugs revealed at extreme close-up. Subtle motion in the second hand or rotor. Surfaces are everything -- brushed steel, polished gold, sapphire crystal, dial lacquer. Mood: Precision, craft, weight. Lighting: Carefully placed strip lights and softboxes shaping each facet. Strong specular control -- no reflection of the camera, no glare blowouts. Dial markers and indices catch a deliberate kicker. Camera: Locked hero frames, slow rotational reveals at 1 frame per second feel. Macro probe lens for tiny details. The watch turns; the camera barely moves. Color grading: Deep blacks, neutral midtones, gold and steel rendered with no color cast. Restrained, faithful to the materials. Print-campaign clarity. Inspired by: heritage Swiss watch brand films, grand-complication watchmaking spots, sculptural sport-luxury watch launches, space-heritage chronograph films.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Precision, craft, weight",
                "artStyle": "Macro horology photography. Watch case shown against a deep gradient, dial detail isolated, crown and lugs revealed at extreme close-up. Subtle motion in the second hand or rotor. Surfaces are everything -- brushed steel, polished gold, sapphire crystal, dial lacquer",
                "lighting": "Carefully placed strip lights and softboxes shaping each facet. Strong specular control -- no reflection of the camera, no glare blowouts. Dial markers and indices catch a deliberate kicker",
                "colorGrading": "Deep blacks, neutral midtones, gold and steel rendered with no color cast. Restrained, faithful to the materials. Print-campaign clarity",
                "colorPalette": ["#0A0A0A", "#1F1F1F", "#8C7A3B", "#E8E8E8", "#1F3A5F"],
            },
            "motion": {
                "camera": "Locked hero frames, slow rotational reveals at 1 frame per second feel. Macro probe lens for tiny details. The watch turns; the camera barely moves"
            },
            "references": [
                "heritage Swiss watch brand films",
                "grand-complication watchmaking spots",
                "sculptural sport-luxury watch launches",
                "space-heritage chronograph films",
            ],
        },
    },
    {
        "id": "sportswear-motion",
        "label": "Sportswear Motion",
        "description": "High-energy sportswear cinematography with athletic motion, sweat, and crisp performance detail. Built for sports brand films, drop campaigns, and athlete features.",
        "category": "commercial",
        "tags": ["commercial", "sportswear", "athletic", "motion", "performance"],
        "promptSuffix": " Art style: Performance photography with athletic intensity. Athletes mid-stride, mid-jump, mid-recovery. Sweat, breath, and chalk caught in suspended motion. Locations include empty stadiums, neon-lit gyms, wet asphalt, indoor courts. Apparel and footwear are featured naturally in performance, not posed. Mood: Kinetic, focused, sweat-and-grit. Lighting: Hard-edged sport lighting -- stadium banks, single high-output soft source, occasional flash for stop-motion crispness. Strong rim light separating athlete from background. Night sequences lit by neon and reflected wet ground. Camera: Slow motion at 120-240fps for hero action. Tracking shots alongside running athletes, low-angle wides for elevation, tight macro on sweat and texture. Quick cuts between disciplines. Color grading: High contrast, slightly desaturated, with one brand accent pushed. Skin tones warm, blacks crushed, highlights crisp. Looks like a global brand spot. Inspired by: global sportswear brand films, engineered running-shoe films, studio-athletic apparel athlete spots, heritage athletic-brand performance films.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Kinetic, focused, sweat-and-grit",
                "artStyle": "Performance photography with athletic intensity. Athletes mid-stride, mid-jump, mid-recovery. Sweat, breath, and chalk caught in suspended motion. Locations include empty stadiums, neon-lit gyms, wet asphalt, indoor courts. Apparel and footwear are featured naturally in performance, not posed",
                "lighting": "Hard-edged sport lighting -- stadium banks, single high-output soft source, occasional flash for stop-motion crispness. Strong rim light separating athlete from background. Night sequences lit by neon and reflected wet ground",
                "colorGrading": "High contrast, slightly desaturated, with one brand accent pushed. Skin tones warm, blacks crushed, highlights crisp. Looks like a global brand spot",
                "colorPalette": ["#0F0F0F", "#FF4F1F", "#F7F7F7", "#1F2937", "#E0E0E0"],
            },
            "motion": {
                "camera": "Slow motion at 120-240fps for hero action. Tracking shots alongside running athletes, low-angle wides for elevation, tight macro on sweat and texture. Quick cuts between disciplines"
            },
            "references": [
                "global sportswear brand films",
                "engineered running-shoe films",
                "studio-athletic apparel athlete spots",
                "heritage athletic-brand performance films",
            ],
        },
    },
    {
        "id": "alcohol-pour",
        "label": "Alcohol Pour",
        "description": "Slow-motion spirits cinematography with controlled splashes, condensation, and bottle hero shots. Built for whisky, gin, tequila, and craft beer brand films.",
        "category": "commercial",
        "tags": ["commercial", "alcohol", "spirits", "pour", "beverage"],
        "promptSuffix": " Art style: Spirits hero cinematography. Bottle on a clean gradient backdrop, glass beside it, single ice cube descending in slow motion. Pour sequences captured at high frame rate, condensation forming, label catching a deliberate kicker. Surfaces are dark wood, slate, or seamless paper. Mood: Crafted, anticipatory, weighty. Lighting: Strong backlight to glow the liquid -- amber, gold, or clear. Rim light shaping bottle silhouette. Negative fill on the opposite side for definition. Dramatic contrast preserved. Camera: Locked hero, slow rotational tracks, macro probe shots into the glass. High frame rate pours -- 480fps for splash, 120fps for the bottle settle. Beats long enough to savor. Color grading: Warm and rich. Amber liquids amplified, deep walnut shadows, golden highlights. Skin tones (if present) warm. Looks like a heritage spirits campaign. Inspired by: heritage single-malt brand films, whimsical botanical gin spots, premium agave-spirit launches, velvety stout-pour films.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Crafted, anticipatory, weighty",
                "artStyle": "Spirits hero cinematography. Bottle on a clean gradient backdrop, glass beside it, single ice cube descending in slow motion. Pour sequences captured at high frame rate, condensation forming, label catching a deliberate kicker. Surfaces are dark wood, slate, or seamless paper",
                "lighting": "Strong backlight to glow the liquid -- amber, gold, or clear. Rim light shaping bottle silhouette. Negative fill on the opposite side for definition. Dramatic contrast preserved",
                "colorGrading": "Warm and rich. Amber liquids amplified, deep walnut shadows, golden highlights. Skin tones (if present) warm. Looks like a heritage spirits campaign",
                "colorPalette": ["#1A0F08", "#B5762C", "#E8B86E", "#3F2A1A", "#FFFFFF"],
            },
            "motion": {
                "camera": "Locked hero, slow rotational tracks, macro probe shots into the glass. High frame rate pours -- 480fps for splash, 120fps for the bottle settle. Beats long enough to savor"
            },
            "references": [
                "heritage single-malt brand films",
                "whimsical botanical gin spots",
                "premium agave-spirit launches",
                "velvety stout-pour films",
            ],
        },
    },
    {
        "id": "jewellery-rotation",
        "label": "Jewellery Rotation",
        "description": "Precise rotational hero shots that show every facet of fine jewellery. Built for diamond, gold, and statement-piece campaigns.",
        "category": "commercial",
        "tags": ["commercial", "jewellery", "jewelry", "macro", "luxury"],
        "promptSuffix": " Art style: Hero jewellery cinematography. A single piece -- ring, necklace, earring -- on a sculpted velvet or stone surface. Hand passes for scale. Surfaces understated so the piece dominates. Brilliance and fire of stones is the protagonist. Mood: Refined, brilliant, deliberate. Lighting: Carefully placed pinpoint lights to ignite facets. Soft ambient base for metalwork. Polarizing controls for unwanted glare. Subtle backlight for stone luminance. Camera: Locked frames, ultra-slow rotational beauty passes, macro probe into stone settings. Hand-model passes shot at 60-120fps. Movement is glacial and deliberate. Color grading: Deep blacks, neutral whites. Metals rendered faithfully, stones with a touch of extra brilliance. No color cast. Print-jewellery polish. Inspired by: legacy diamond-house brand films, Parisian high-jewellery maison spots, whimsical fine-jewellery craft films, bold Roman jewellery heritage reels.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Refined, brilliant, deliberate",
                "artStyle": "Hero jewellery cinematography. A single piece -- ring, necklace, earring -- on a sculpted velvet or stone surface. Hand passes for scale. Surfaces understated so the piece dominates. Brilliance and fire of stones is the protagonist",
                "lighting": "Carefully placed pinpoint lights to ignite facets. Soft ambient base for metalwork. Polarizing controls for unwanted glare. Subtle backlight for stone luminance",
                "colorGrading": "Deep blacks, neutral whites. Metals rendered faithfully, stones with a touch of extra brilliance. No color cast. Print-jewellery polish",
                "colorPalette": ["#0A0A0A", "#1A1A1A", "#E8C766", "#F0F0F0", "#B23A48"],
            },
            "motion": {
                "camera": "Locked frames, ultra-slow rotational beauty passes, macro probe into stone settings. Hand-model passes shot at 60-120fps. Movement is glacial and deliberate"
            },
            "references": [
                "legacy diamond-house brand films",
                "Parisian high-jewellery maison spots",
                "whimsical fine-jewellery craft films",
                "bold Roman jewellery heritage reels",
            ],
        },
    },
    {
        "id": "hospitality-lifestyle",
        "label": "Hospitality Lifestyle",
        "description": "Aspirational hotel and resort cinematography with soft warm light, infinity pools, breakfast on linen, and unhurried bodies in beautiful spaces. Built for hospitality brand films.",
        "category": "commercial",
        "tags": ["commercial", "hospitality", "hotel", "resort", "travel"],
        "promptSuffix": " Art style: Editorial hospitality cinematography. Guests at ease in beautifully designed rooms, infinity pools meeting horizons, breakfasts on linen with a sea view, cocktails at sunset. Architecture, materials, and natural surroundings carry equal weight with people. Mood: Calm, restorative, indulgent. Lighting: Soft natural daylight, warm interior practicals at evening, soft evening sun for exteriors. Wraparound bounce keeps shadows luminous. Never harsh -- the space always feels welcoming. Camera: Slow gimbal and tracked moves through interiors and grounds. Aerial reveals of architecture and landscape. Intimate medium shots for guests, wide masters for environment. Long beats that let the place breathe. Color grading: Warm filmic with creamy highlights. Skin tones soft and golden. Natural greens and blues lightly enhanced. Looks like a travel-magazine spread in motion. Inspired by: serene ultra-luxury resort brand films, wellness-resort spots, members-club lifestyle films, heritage rail-and-resort films.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Calm, restorative, indulgent",
                "artStyle": "Editorial hospitality cinematography. Guests at ease in beautifully designed rooms, infinity pools meeting horizons, breakfasts on linen with a sea view, cocktails at sunset. Architecture, materials, and natural surroundings carry equal weight with people",
                "lighting": "Soft natural daylight, warm interior practicals at evening, soft evening sun for exteriors. Wraparound bounce keeps shadows luminous. Never harsh -- the space always feels welcoming",
                "colorGrading": "Warm filmic with creamy highlights. Skin tones soft and golden. Natural greens and blues lightly enhanced. Looks like a travel-magazine spread in motion",
                "colorPalette": ["#F5EAD8", "#A6886F", "#3D2A1F", "#8DB6B8", "#FFFFFF"],
            },
            "motion": {
                "camera": "Slow gimbal and tracked moves through interiors and grounds. Aerial reveals of architecture and landscape. Intimate medium shots for guests, wide masters for environment. Long beats that let the place breathe"
            },
            "references": [
                "serene ultra-luxury resort brand films",
                "wellness-resort spots",
                "members-club lifestyle films",
                "heritage rail-and-resort films",
            ],
        },
    },
    {
        "id": "white-background-studio",
        "label": "White Background Studio",
        "description": "Clean catalogue-style product shots on pure white seamless. Built for marketplace listings, PDPs, and DTC product pages where the product alone has to sell.",
        "category": "ecommerce",
        "tags": ["ecommerce", "studio", "white-bg", "catalogue", "pdp"],
        "promptSuffix": " Art style: Pure white seamless studio. The product is centered, perfectly exposed, and shown from the angles a buyer needs -- front three-quarter, profile, top-down, detail. No props beyond what is essential. The product, exactly as it ships, is the protagonist. Mood: Clean, neutral, honest. Lighting: Two large softboxes flanking the product at 45 degrees with a top light for shadow control. Background lit to clean white without spill onto the product. Even, neutral, flattering. No drama. Camera: Locked hero frames at each required angle. Slow rotational beauty pass. Macro detail passes for label, stitching, finish. No handheld energy, no creative angles. Color grading: Neutral and true-to-life. Whites are clean and white, color rendered faithfully. No stylization. Buyer-trustworthy color accuracy. Inspired by: Amazon PDP Imagery, luxury fashion e-commerce product pages, premium consumer-electronics store imagery, e-commerce storefront template imagery.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Clean, neutral, honest",
                "artStyle": "Pure white seamless studio. The product is centered, perfectly exposed, and shown from the angles a buyer needs -- front three-quarter, profile, top-down, detail. No props beyond what is essential. The product, exactly as it ships, is the protagonist",
                "lighting": "Two large softboxes flanking the product at 45 degrees with a top light for shadow control. Background lit to clean white without spill onto the product. Even, neutral, flattering. No drama",
                "colorGrading": "Neutral and true-to-life. Whites are clean and white, color rendered faithfully. No stylization. Buyer-trustworthy color accuracy",
                "colorPalette": ["#FFFFFF", "#F5F5F5", "#1A1A1A", "#9CA3AF", "#E5E7EB"],
            },
            "motion": {
                "camera": "Locked hero frames at each required angle. Slow rotational beauty pass. Macro detail passes for label, stitching, finish. No handheld energy, no creative angles"
            },
            "references": [
                "Amazon PDP Imagery",
                "luxury fashion e-commerce product pages",
                "premium consumer-electronics store imagery",
                "e-commerce storefront template imagery",
            ],
        },
    },
    {
        "id": "lifestyle-on-model",
        "label": "Lifestyle On-Model",
        "description": "Product in use on real-world talent in everyday context -- worn, held, applied, lived in. Built for PDP secondary images, social cuts, and email hero.",
        "category": "ecommerce",
        "tags": ["ecommerce", "lifestyle", "on-model", "pdp", "social"],
        "promptSuffix": " Art style: On-model lifestyle photography. Talent wears or uses the product in a believable environment -- the product is integrated, never floating. Wardrobe and prop choices feel like the customer’s own life rather than a stylist’s mood board. Modest art direction. Mood: Natural, relatable, warm. Lighting: Soft natural daylight, occasional reflector fill for face. Outdoor and indoor both supported. Highlights gentle, never blown. Camera: Mix of locked and lightly-floated handheld. Medium shots for context, close-ups for product interaction, occasional wide for environment. Pace is unhurried. Color grading: Warm naturalism. Skin tones honest, shadows soft, whites slightly cream. Product color faithful. Looks like a friendly catalogue. Inspired by: transparent-basics DTC product pages, understated minimalist fashion lookbooks, outdoor-apparel brand spots, natural-material footwear brand content.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Natural, relatable, warm",
                "artStyle": "On-model lifestyle photography. Talent wears or uses the product in a believable environment -- the product is integrated, never floating. Wardrobe and prop choices feel like the customer’s own life rather than a stylist’s mood board. Modest art direction",
                "lighting": "Soft natural daylight, occasional reflector fill for face. Outdoor and indoor both supported. Highlights gentle, never blown",
                "colorGrading": "Warm naturalism. Skin tones honest, shadows soft, whites slightly cream. Product color faithful. Looks like a friendly catalogue",
                "colorPalette": ["#F8EFE0", "#C5A88D", "#3F3F3F", "#8FA9B5", "#FFFFFF"],
            },
            "motion": {
                "camera": "Mix of locked and lightly-floated handheld. Medium shots for context, close-ups for product interaction, occasional wide for environment. Pace is unhurried"
            },
            "references": [
                "transparent-basics DTC product pages",
                "understated minimalist fashion lookbooks",
                "outdoor-apparel brand spots",
                "natural-material footwear brand content",
            ],
        },
    },
    {
        "id": "ugc-unboxing",
        "label": "UGC Unboxing",
        "description": "Authentic-feeling unboxing from a real-customer POV. Built for social ads, organic short-form, and influencer-style content that performs like UGC.",
        "category": "ecommerce",
        "tags": ["ecommerce", "ugc", "unboxing", "social", "authentic"],
        "promptSuffix": " Art style: Phone-shot unboxing aesthetic. Box on a coffee table, packing tape pulled, layers of tissue and protective material revealed, product lifted into frame. Hands are the only person on camera. Environment is a real apartment or office -- visible plant, mug, laptop. Mood: Excited, casual, authentic. Lighting: Natural window light or an inexpensive ring light. Never studio-perfect. Some shadow, some warmth, occasional auto-exposure shifts. Camera: Handheld phone POV from chest height. Slight micro-jitter. Occasional reframing as the action proceeds. Quick cuts that mimic a creator’s editing rhythm. Native vertical framing. Color grading: Auto-WB digital look. Slightly oversharpened, slightly soft. Highlights occasionally clipped. Looks like a phone camera roll. Inspired by: TikTok Creator Unboxings, Reels Product Reveals, Amazon Live Streams, Influencer Haul Videos.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Excited, casual, authentic",
                "artStyle": "Phone-shot unboxing aesthetic. Box on a coffee table, packing tape pulled, layers of tissue and protective material revealed, product lifted into frame. Hands are the only person on camera. Environment is a real apartment or office -- visible plant, mug, laptop",
                "lighting": "Natural window light or an inexpensive ring light. Never studio-perfect. Some shadow, some warmth, occasional auto-exposure shifts",
                "colorGrading": "Auto-WB digital look. Slightly oversharpened, slightly soft. Highlights occasionally clipped. Looks like a phone camera roll",
                "colorPalette": ["#F5EFE5", "#3A3A3A", "#D85F4A", "#FFFFFF", "#A8AFB8"],
            },
            "motion": {
                "camera": "Handheld phone POV from chest height. Slight micro-jitter. Occasional reframing as the action proceeds. Quick cuts that mimic a creator’s editing rhythm. Native vertical framing"
            },
            "references": [
                "TikTok Creator Unboxings",
                "Reels Product Reveals",
                "Amazon Live Streams",
                "Influencer Haul Videos",
            ],
        },
    },
    {
        "id": "flat-lay-overhead",
        "label": "Flat-Lay Overhead",
        "description": "Composed flat-lay product photography from directly overhead. Built for editorial PDPs, social posts, and category banners where styling sells the story.",
        "category": "ecommerce",
        "tags": ["ecommerce", "flat-lay", "overhead", "styling", "editorial"],
        "promptSuffix": " Art style: Top-down flat-lay composition. Product surrounded by a curated arrangement of complementary props -- a coffee, a notebook, a sprig of herb, folded linen. Negative space respected. Surfaces are honest -- raw wood, marble, linen, paper. Mood: Composed, calm, considered. Lighting: Single soft overhead window-like source, slight directional shadow that anchors objects. Even, gentle, no spill onto the lens. Camera: Locked overhead camera. Subtle dolly-in or push-out. Slow rotational reframes around the centerpiece. Occasional reveal of a hand placing or adjusting an element. Color grading: Warm naturalism. Subtle film grain texture. Highlights soft, shadows luminous. Looks like a stylist’s portfolio. Inspired by: slow-living lifestyle-magazine spreads, houseplant e-commerce editorial, editorial top-down recipe videos, apothecary-style catalogue imagery.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Composed, calm, considered",
                "artStyle": "Top-down flat-lay composition. Product surrounded by a curated arrangement of complementary props -- a coffee, a notebook, a sprig of herb, folded linen. Negative space respected. Surfaces are honest -- raw wood, marble, linen, paper",
                "lighting": "Single soft overhead window-like source, slight directional shadow that anchors objects. Even, gentle, no spill onto the lens",
                "colorGrading": "Warm naturalism. Subtle film grain texture. Highlights soft, shadows luminous. Looks like a stylist’s portfolio",
                "colorPalette": ["#F5EDE0", "#A18258", "#3F3F3F", "#D9E3D4", "#FFFFFF"],
            },
            "motion": {
                "camera": "Locked overhead camera. Subtle dolly-in or push-out. Slow rotational reframes around the centerpiece. Occasional reveal of a hand placing or adjusting an element"
            },
            "references": [
                "slow-living lifestyle-magazine spreads",
                "houseplant e-commerce editorial",
                "editorial top-down recipe videos",
                "apothecary-style catalogue imagery",
            ],
        },
    },
    {
        "id": "360-turntable",
        "label": "360 Turntable",
        "description": "Rotational beauty pass that shows the product from every angle in a single move. Built for marketplace hero modules, PDP turntables, and AR-style preview content.",
        "category": "ecommerce",
        "tags": ["ecommerce", "turntable", "360", "rotation", "pdp"],
        "promptSuffix": " Art style: Product on a seamless gradient backdrop, viewed from a fixed camera as it slowly rotates a full 360 degrees. No props, no distractions. Every angle visible in a single move. Product centered and consistently exposed throughout the rotation. Mood: Confident, complete, mechanical. Lighting: Three-point softbox setup that holds even illumination across all rotational angles. Subtle rim light from above. Background lit to gentle gradient. Camera: Fully locked camera, product rotates on a motorized turntable. Single take, single move, smooth constant velocity. No cuts. Color grading: Neutral, faithful, even. No stylization. Buyer-trustworthy. Inspired by: AR product-preview turntables, electronics-retail PDP turntables, 3D product-showcase viewers, Sneaker Resale Detail Reels.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Confident, complete, mechanical",
                "artStyle": "Product on a seamless gradient backdrop, viewed from a fixed camera as it slowly rotates a full 360 degrees. No props, no distractions. Every angle visible in a single move. Product centered and consistently exposed throughout the rotation",
                "lighting": "Three-point softbox setup that holds even illumination across all rotational angles. Subtle rim light from above. Background lit to gentle gradient",
                "colorGrading": "Neutral, faithful, even. No stylization. Buyer-trustworthy",
                "colorPalette": ["#F5F5F5", "#1A1A1A", "#9CA3AF", "#FFFFFF", "#D1D5DB"],
            },
            "motion": {
                "camera": "Fully locked camera, product rotates on a motorized turntable. Single take, single move, smooth constant velocity. No cuts"
            },
            "references": [
                "AR product-preview turntables",
                "electronics-retail PDP turntables",
                "3D product-showcase viewers",
                "Sneaker Resale Detail Reels",
            ],
        },
    },
    {
        "id": "as-seen-on-phone",
        "label": "As Seen On Phone",
        "description": "Product shot framed inside a phone screen, mimicking how the customer will actually see it scrolling. Built for ad creative, social-first launches, and conversion tests.",
        "category": "ecommerce",
        "tags": ["ecommerce", "social", "phone", "mockup", "ad"],
        "promptSuffix": " Art style: A single phone (often held in-hand in a real environment) showing a vertical product video or PDP. Bezel and notch present. Thumb sometimes enters frame to scroll or tap. Background environment is unobtrusive but real -- a kitchen counter, a desk, a sofa. Mood: Native, modern, mobile-first. Lighting: Soft natural daylight on the phone-holding hand, screen glow slightly reflected on fingers. Phone screen renders at full brightness. Camera: Mostly locked over-the-shoulder onto the phone. Occasional reframe as the user scrolls. Macro detail when the product hits the feed. Cuts mirror typical short-form pacing. Color grading: Neutral environment, screen color faithful. Slight warmth on hand and surface. Looks like a phone in real use. Inspired by: flagship smartphone ads, Meta Ad Creative Templates, TikTok Spark Ads, Mobile-Native Brand Films.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Native, modern, mobile-first",
                "artStyle": "A single phone (often held in-hand in a real environment) showing a vertical product video or PDP. Bezel and notch present. Thumb sometimes enters frame to scroll or tap. Background environment is unobtrusive but real -- a kitchen counter, a desk, a sofa",
                "lighting": "Soft natural daylight on the phone-holding hand, screen glow slightly reflected on fingers. Phone screen renders at full brightness",
                "colorGrading": "Neutral environment, screen color faithful. Slight warmth on hand and surface. Looks like a phone in real use",
                "colorPalette": ["#F4F4F4", "#1A1A1A", "#3478F6", "#FFFFFF", "#A8AFB8"],
            },
            "motion": {
                "camera": "Mostly locked over-the-shoulder onto the phone. Occasional reframe as the user scrolls. Macro detail when the product hits the feed. Cuts mirror typical short-form pacing"
            },
            "references": [
                "flagship smartphone ads",
                "Meta Ad Creative Templates",
                "TikTok Spark Ads",
                "Mobile-Native Brand Films",
            ],
        },
    },
    {
        "id": "marketplace-listing",
        "label": "Marketplace Listing",
        "description": "Strict, marketplace-compliant product imagery -- pure white, single product, no overlays. Built for Amazon, Etsy, and aggregator listings.",
        "category": "ecommerce",
        "tags": ["ecommerce", "marketplace", "amazon", "compliance", "listing"],
        "promptSuffix": " Art style: Single product on pure white. No props, no text overlays, no watermarks, no humans, no other items. The product fills 85 percent of the frame. Standard PDP angles -- main, alternate angles, scale reference, in-package detail. Mood: Disciplined, unembellished, compliant. Lighting: Two large softboxes flanking the product, top light for shadow control, white-card fill below. Even illumination, no dramatic shadow. Background blown to pure white. Camera: Locked hero frames for each required angle. No movement. No cuts within a frame. Square aspect for thumbnails. Color grading: Neutral, true to life. No filter, no contrast push. Pure compliant clarity. Inspired by: Amazon Listing Standards, Etsy Product Photography Guide, Walmart Marketplace PDPs, eBay Hero Imagery.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Disciplined, unembellished, compliant",
                "artStyle": "Single product on pure white. No props, no text overlays, no watermarks, no humans, no other items. The product fills 85 percent of the frame. Standard PDP angles -- main, alternate angles, scale reference, in-package detail",
                "lighting": "Two large softboxes flanking the product, top light for shadow control, white-card fill below. Even illumination, no dramatic shadow. Background blown to pure white",
                "colorGrading": "Neutral, true to life. No filter, no contrast push. Pure compliant clarity",
                "colorPalette": ["#FFFFFF", "#F5F5F5", "#1A1A1A", "#9CA3AF", "#E5E7EB"],
            },
            "motion": {
                "camera": "Locked hero frames for each required angle. No movement. No cuts within a frame. Square aspect for thumbnails"
            },
            "references": [
                "Amazon Listing Standards",
                "Etsy Product Photography Guide",
                "Walmart Marketplace PDPs",
                "eBay Hero Imagery",
            ],
        },
    },
    {
        "id": "kitchen-counter-ugc",
        "label": "Kitchen Counter UGC",
        "description": "Authentic-feeling UGC of food, kitchen tools, or supplements shot in a real home kitchen. Built for CPG social ads and food/beverage organic short-form.",
        "category": "ecommerce",
        "tags": ["ecommerce", "ugc", "kitchen", "cpg", "authentic"],
        "promptSuffix": " Art style: Real-home kitchen setting. Product on the counter alongside everyday items -- a mug, a half-cut lemon, a tea towel. Hand sometimes enters to scoop, pour, or stir. Slight visual clutter signals authenticity rather than studio polish. Mood: Genuine, lived-in, energetic. Lighting: Available daylight from a kitchen window or a single off-axis overhead. Mixed temperature accepted -- the world isn’t a studio. Camera: Handheld phone framing, slight micro-jitter, occasional refocus hunt. Mix of medium shots and macro detail. Pacing matches creator-style edit. Color grading: Auto-WB digital look. Slight oversharpening, slightly cooler shadows. No professional grade. Looks like a creator just hit post. Inspired by: TikTok Recipe Creators, Reels Cookware Demos, YouTube Shorts Kitchen Reviews, Instagram Food Hauls.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Genuine, lived-in, energetic",
                "artStyle": "Real-home kitchen setting. Product on the counter alongside everyday items -- a mug, a half-cut lemon, a tea towel. Hand sometimes enters to scoop, pour, or stir. Slight visual clutter signals authenticity rather than studio polish",
                "lighting": "Available daylight from a kitchen window or a single off-axis overhead. Mixed temperature accepted -- the world isn’t a studio",
                "colorGrading": "Auto-WB digital look. Slight oversharpening, slightly cooler shadows. No professional grade. Looks like a creator just hit post",
                "colorPalette": ["#F7EFE3", "#8FA98D", "#3F3F3F", "#D85F4A", "#FFFFFF"],
            },
            "motion": {
                "camera": "Handheld phone framing, slight micro-jitter, occasional refocus hunt. Mix of medium shots and macro detail. Pacing matches creator-style edit"
            },
            "references": [
                "TikTok Recipe Creators",
                "Reels Cookware Demos",
                "YouTube Shorts Kitchen Reviews",
                "Instagram Food Hauls",
            ],
        },
    },
    {
        "id": "bathroom-counter-ugc",
        "label": "Bathroom Counter UGC",
        "description": "Authentic-feeling beauty UGC shot on a real bathroom counter -- skincare, haircare, cosmetics in their natural habitat. Built for beauty social ads and creator-style content.",
        "category": "ecommerce",
        "tags": ["ecommerce", "ugc", "bathroom", "beauty", "skincare"],
        "promptSuffix": " Art style: Bathroom counter or vanity setting. Product among other believable items -- toothbrush, hair tie, a candle. Hand enters frame to dispense, apply, or swatch. A mirror occasionally visible. Routine feels like a real morning. Mood: Intimate, daily-ritual, real. Lighting: Available bathroom lighting -- overhead vanity bar, plus daylight if a window is present. Mixed temperature, sometimes slightly warm-tinted. Camera: Phone-shot POV from chest height with slight handheld energy. Macro detail on swatches and texture. Occasional self-shot mirror frames. Native vertical aspect. Color grading: Auto-WB digital look. Slight pink/cream cast typical of bathroom lighting. Skin tones warm. Looks like a real morning routine. Inspired by: Skincare TikTok Routines, Beauty Reels Demos, beauty-retailer creator-squad content, Influencer GRWM Videos.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Intimate, daily-ritual, real",
                "artStyle": "Bathroom counter or vanity setting. Product among other believable items -- toothbrush, hair tie, a candle. Hand enters frame to dispense, apply, or swatch. A mirror occasionally visible. Routine feels like a real morning",
                "lighting": "Available bathroom lighting -- overhead vanity bar, plus daylight if a window is present. Mixed temperature, sometimes slightly warm-tinted",
                "colorGrading": "Auto-WB digital look. Slight pink/cream cast typical of bathroom lighting. Skin tones warm. Looks like a real morning routine",
                "colorPalette": ["#F8EDE3", "#D6A48F", "#3F3F3F", "#A8C0B5", "#FFFFFF"],
            },
            "motion": {
                "camera": "Phone-shot POV from chest height with slight handheld energy. Macro detail on swatches and texture. Occasional self-shot mirror frames. Native vertical aspect"
            },
            "references": [
                "Skincare TikTok Routines",
                "Beauty Reels Demos",
                "beauty-retailer creator-squad content",
                "Influencer GRWM Videos",
            ],
        },
    },
    {
        "id": "returns-friendly-diagnostic",
        "label": "Returns-Friendly Diagnostic",
        "description": 'Shows a product at true scale from every side — next to real-world objects like a hand, a tape measure, or a phone — with close-ups of fit and material. The honest "exactly what you get" look that cuts returns.',
        "category": "ecommerce",
        "tags": ["ecommerce", "diagnostic", "fit", "scale", "returns"],
        "promptSuffix": " Art style: Product photographed with explicit scale and dimension cues -- next to a hand, a tape measure, or a recognizable object. Apparel shown worn on a believable body, the cut and drape reading clearly. Footwear shown with sole, profile, and stack-height detail visible. Materials shown at macro to show weave and weight. Mood: Honest, helpful, thorough. Lighting: Even soft studio key with subtle directional fill. Color rendering accurate -- a fabric’s true color must read. Macro detail lit to show texture. Camera: Locked frame on the product. Slow rotational beauty pass. Macro detail at material grain. A recognizable object held alongside for scale reference. Color grading: Neutral and faithful. No stylization. Print-ready color accuracy. Inspired by: footwear-retail PDP imagery, menswear fit-guide imagery, fashion e-commerce catwalk loops, outdoor-gear technical product notes.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Honest, helpful, thorough",
                "artStyle": "Product photographed with explicit scale and dimension cues -- next to a hand, a tape measure, or a recognizable object. Apparel shown worn on a believable body, the cut and drape reading clearly. Footwear shown with sole, profile, and stack-height detail visible. Materials shown at macro to show weave and weight",
                "lighting": "Even soft studio key with subtle directional fill. Color rendering accurate -- a fabric’s true color must read. Macro detail lit to show texture",
                "colorGrading": "Neutral and faithful. No stylization. Print-ready color accuracy",
                "colorPalette": ["#F4F4F4", "#1A1A1A", "#6B7280", "#FFFFFF", "#E5E7EB"],
            },
            "motion": {
                "camera": "Locked frame on the product. Slow rotational beauty pass. Macro detail at material grain. A recognizable object held alongside for scale reference"
            },
            "references": [
                "footwear-retail PDP imagery",
                "menswear fit-guide imagery",
                "fashion e-commerce catwalk loops",
                "outdoor-gear technical product notes",
            ],
        },
    },
    {
        "id": "packaging-close-up",
        "label": "Packaging Close-Up",
        "description": "Macro packaging cinematography that lingers on print finish, foil, emboss, and material. Built for craft brands, premium DTC, and unboxing-driven campaigns.",
        "category": "ecommerce",
        "tags": ["ecommerce", "packaging", "macro", "craft", "brand"],
        "promptSuffix": " Art style: Macro photography of packaging detail. Embossed logos catching raking light, foil stamping reflecting deliberately, paper grain visible, ribbon edges sharp. The product is implied; the package itself is the protagonist. Mood: Crafted, tactile, considered. Lighting: Single raking light low across the surface to reveal emboss and texture. Soft fill from the opposite side. Glints on foil controlled with strip light. Camera: Locked frames or slow rotational moves. Macro probe lens at edges and seams. Beats held long enough to feel the craft. Color grading: Warm, rich, with deep blacks and faithful highlights. Foils rendered with true gleam. Looks like an art-book print. Inspired by: premium electronics packaging reveals, apothecary box films, French candle-house packaging spots, Boutique Spirits Box Films.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Crafted, tactile, considered",
                "artStyle": "Macro photography of packaging detail. Embossed logos catching raking light, foil stamping reflecting deliberately, paper grain visible, ribbon edges sharp. The product is implied; the package itself is the protagonist",
                "lighting": "Single raking light low across the surface to reveal emboss and texture. Soft fill from the opposite side. Glints on foil controlled with strip light",
                "colorGrading": "Warm, rich, with deep blacks and faithful highlights. Foils rendered with true gleam. Looks like an art-book print",
                "colorPalette": ["#F2EBDD", "#1F1A14", "#C8A464", "#7F1D1D", "#FFFFFF"],
            },
            "motion": {
                "camera": "Locked frames or slow rotational moves. Macro probe lens at edges and seams. Beats held long enough to feel the craft"
            },
            "references": [
                "premium electronics packaging reveals",
                "apothecary box films",
                "French candle-house packaging spots",
                "Boutique Spirits Box Films",
            ],
        },
    },
    {
        "id": "in-context-use",
        "label": "In-Context Use",
        "description": "Product shown solving its actual problem in its actual environment -- the cooler at the picnic, the bag on the bike, the lamp on the desk. Built for benefits-led ad creative.",
        "category": "ecommerce",
        "tags": ["ecommerce", "in-context", "use-case", "benefits", "ad"],
        "promptSuffix": " Art style: Product captured solving a real problem in a real place. A backpack loaded for a flight, a kettle pouring into a mug at the kitchen counter, a lamp illuminating a desk at dusk. Surrounding context is honest. The benefit is shown, not stated. Mood: Practical, situational, persuasive. Lighting: Available natural and practical lighting matched to the scene. Soft fill where faces are present. Believable, never staged. Camera: Mix of medium shots showing context and tight inserts showing function. Light handheld energy. Cuts driven by the action -- pour, click, zip, swipe. Color grading: Warm naturalism with slight contrast lift. Skin tones honest. Looks like a believable day, not a studio. Inspired by: rugged outdoor-cooler brand films, repair-and-reuse outdoor-apparel stories, carry-goods function films, mail-in knife-sharpening spots.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Practical, situational, persuasive",
                "artStyle": "Product captured solving a real problem in a real place. A backpack loaded for a flight, a kettle pouring into a mug at the kitchen counter, a lamp illuminating a desk at dusk. Surrounding context is honest. The benefit is shown, not stated",
                "lighting": "Available natural and practical lighting matched to the scene. Soft fill where faces are present. Believable, never staged",
                "colorGrading": "Warm naturalism with slight contrast lift. Skin tones honest. Looks like a believable day, not a studio",
                "colorPalette": ["#F5EDE0", "#3F3F3F", "#A18258", "#8AA39B", "#FFFFFF"],
            },
            "motion": {
                "camera": "Mix of medium shots showing context and tight inserts showing function. Light handheld energy. Cuts driven by the action -- pour, click, zip, swipe"
            },
            "references": [
                "rugged outdoor-cooler brand films",
                "repair-and-reuse outdoor-apparel stories",
                "carry-goods function films",
                "mail-in knife-sharpening spots",
            ],
        },
    },
    {
        "id": "warm-vlog",
        "label": "Warm Vlog",
        "description": "Cozy at-home vlog aesthetic with warm practicals, soft handheld energy, and direct-to-camera intimacy. Built for creator-style brand films and personal narratives.",
        "category": "influencer",
        "tags": ["influencer", "vlog", "talking-head", "home", "cozy"],
        "promptSuffix": " Art style: Creator-style at-home vlog. Subject talks to camera from a sofa, desk, or kitchen island with the lived-in clutter of a real apartment behind them -- plants, books, a coffee. Wardrobe casual. Set design is the actual room. Mood: Warm, intimate, conversational. Lighting: Soft natural daylight through a window plus warm tungsten practicals -- a lamp, fairy lights, candles. Mixed temperature embraced. Subject lit gently from the side, never flat. Camera: Light handheld locked-off on a tripod with the occasional reframe. Medium and close-up framing. Cuts driven by edit pace, b-roll inserts of hands, mug, laptop, dog. Native vertical or 16:9 supported. Color grading: Warm filmic. Cream highlights, soft shadows, gentle saturation. Skin tones warm and forgiving. Looks like a thoughtful creator’s grade. Inspired by: kinetic daily-vlog creator energy, ironic intimate lifestyle-creator films, minimalist slow-living vlog episodes, wellness-creator lifestyle content.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Warm, intimate, conversational",
                "artStyle": "Creator-style at-home vlog. Subject talks to camera from a sofa, desk, or kitchen island with the lived-in clutter of a real apartment behind them -- plants, books, a coffee. Wardrobe casual. Set design is the actual room",
                "lighting": "Soft natural daylight through a window plus warm tungsten practicals -- a lamp, fairy lights, candles. Mixed temperature embraced. Subject lit gently from the side, never flat",
                "colorGrading": "Warm filmic. Cream highlights, soft shadows, gentle saturation. Skin tones warm and forgiving. Looks like a thoughtful creator’s grade",
                "colorPalette": ["#F4E4CF", "#A4805D", "#3F2A1A", "#D9A78A", "#FFFFFF"],
            },
            "motion": {
                "camera": "Light handheld locked-off on a tripod with the occasional reframe. Medium and close-up framing. Cuts driven by edit pace, b-roll inserts of hands, mug, laptop, dog. Native vertical or 16:9 supported"
            },
            "references": [
                "kinetic daily-vlog creator energy",
                "ironic intimate lifestyle-creator films",
                "minimalist slow-living vlog episodes",
                "wellness-creator lifestyle content",
            ],
        },
    },
    {
        "id": "ring-light-tutorial",
        "label": "Ring-Light Tutorial",
        "description": "Direct-to-camera tutorial aesthetic with even ring-light fill, clean background, and tight talking-head framing. Built for how-tos, product demos, and creator education.",
        "category": "influencer",
        "tags": ["influencer", "tutorial", "ring-light", "talking-head", "demo"],
        "promptSuffix": " Art style: Creator at a desk or vanity speaking directly to camera, lit by an off-camera ring light -- its signature circular catchlight shows in their eyes, but the ring light fixture itself is never in the shot. Background tidy but personal -- a shelf, a piece of art, a houseplant. Occasional cut to overhead hands or insert of the product being demoed. Mood: Clear, instructional, friendly. Lighting: Off-camera ring light as key -- read it only through the circular catchlight in the eyes and an even, frontal, flattering wash; never show the ring light fixture or its stand in frame. Soft ambient fill from the room. No dramatic shadow. Camera: Locked-off tripod head-and-shoulders frame. Occasional re-zoom for emphasis. Insert cuts to overhead hand demos. Native vertical aspect. Color grading: Bright, even, slightly warm. Skin tones flattering. Whites slightly cream. Looks like a polished but personal tutorial. Inspired by: TikTok Beauty Tutorials, YouTube Shorts How-Tos, Reels Skincare Routines, Creator-Led Brand Tutorials.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Clear, instructional, friendly",
                "artStyle": "Creator at a desk or vanity speaking directly to camera, lit by an off-camera ring light -- its signature circular catchlight shows in their eyes, but the ring light fixture itself is never in the shot. Background tidy but personal -- a shelf, a piece of art, a houseplant. Occasional cut to overhead hands or insert of the product being demoed",
                "lighting": "Off-camera ring light as key -- read it only through the circular catchlight in the eyes and an even, frontal, flattering wash; never show the ring light fixture or its stand in frame. Soft ambient fill from the room. No dramatic shadow",
                "colorGrading": "Bright, even, slightly warm. Skin tones flattering. Whites slightly cream. Looks like a polished but personal tutorial",
                "colorPalette": ["#F7EFE3", "#F0AAB7", "#1F1A14", "#8AA39B", "#FFFFFF"],
            },
            "motion": {
                "camera": "Locked-off tripod head-and-shoulders frame. Occasional re-zoom for emphasis. Insert cuts to overhead hand demos. Native vertical aspect"
            },
            "references": [
                "TikTok Beauty Tutorials",
                "YouTube Shorts How-Tos",
                "Reels Skincare Routines",
                "Creator-Led Brand Tutorials",
            ],
        },
    },
    {
        "id": "podcast-clip",
        "label": "Podcast Clip",
        "description": "Studio-podcast aesthetic with broadcast mic, dual angles, and warm directional light. Built for repurposed-podcast short-form and creator interview series.",
        "category": "influencer",
        "tags": ["influencer", "podcast", "interview", "clip", "broadcast"],
        "promptSuffix": " Art style: Two-host podcast setup at a wood-top table with broadcast mics on boom arms. Background features warm shelving, a soft neon glow, plants, or acoustic panels. Hosts wear neutral wardrobe. A single wide two-shot of both hosts at the table. Mood: Considered, conversational, broadcast-grade. Lighting: Warm directional key from one side with cool ambient fill from the opposite. Mics catch a deliberate kicker. Background lit moodily but legibly. Camera: Locked broadcast framing on the hosts at the table. Slow lens-zoom punch-in for emphasis. Color grading: Warm, rich, low-contrast filmic. Highlights bloomed, blacks lifted slightly. Skin tones warm. Looks like a content studio shoot. Inspired by: long-form studio podcast conversations, executive-interview podcast sets, celebrity-friends banter podcast studios, confessional pop-culture podcasts.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Considered, conversational, broadcast-grade",
                "artStyle": "Two-host podcast setup at a wood-top table with broadcast mics on boom arms. Background features warm shelving, a soft neon glow, plants, or acoustic panels. Hosts wear neutral wardrobe. A single wide two-shot of both hosts at the table",
                "lighting": "Warm directional key from one side with cool ambient fill from the opposite. Mics catch a deliberate kicker. Background lit moodily but legibly",
                "colorGrading": "Warm, rich, low-contrast filmic. Highlights bloomed, blacks lifted slightly. Skin tones warm. Looks like a content studio shoot",
                "colorPalette": ["#1A1410", "#7F3F2C", "#E6C28C", "#3D2A1F", "#FFFFFF"],
            },
            "motion": {
                "camera": "Locked broadcast framing on the hosts at the table. Slow lens-zoom punch-in for emphasis"
            },
            "references": [
                "long-form studio podcast conversations",
                "executive-interview podcast sets",
                "celebrity-friends banter podcast studios",
                "confessional pop-culture podcasts",
            ],
        },
    },
    {
        "id": "street-interview",
        "label": "Street Interview",
        "description": "On-the-street talking-head with handheld camera, real ambient sound vibe, and unscripted-feeling reactions. Built for man-on-the-street ads and organic reportage content.",
        "category": "influencer",
        "tags": ["influencer", "street", "interview", "man-on-the-street", "reportage"],
        "promptSuffix": " Art style: Subject standing in a busy urban setting -- a sidewalk, a park, a market -- being interviewed off-camera. Handheld mic sometimes visible in frame. Real passersby and ambient detail. Often shot with a presenter visible in cutaway. Mood: Spontaneous, unscripted, in-the-moment. Lighting: Available daylight. Subject backlit by sun or front-lit on overcast. Camera adjusts exposure on the fly. No fill, no diffusion. Real world only. Camera: Handheld camera at chest height with subtle micro-shake. Frequent reframes as the subject reacts. Cuts to b-roll of the location. Native vertical aspect for short-form repurposing. Color grading: Natural with slight digital crunch. Skin tones honest. Highlights occasionally rolled off. Reads like a news package. Inspired by: optimistic explainer-journalist street vox, broadcast-news vox pops, TikTok Street Interviews, between-takes interview-show cutaways.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Spontaneous, unscripted, in-the-moment",
                "artStyle": "Subject standing in a busy urban setting -- a sidewalk, a park, a market -- being interviewed off-camera. Handheld mic sometimes visible in frame. Real passersby and ambient detail. Often shot with a presenter visible in cutaway",
                "lighting": "Available daylight. Subject backlit by sun or front-lit on overcast. Camera adjusts exposure on the fly. No fill, no diffusion. Real world only",
                "colorGrading": "Natural with slight digital crunch. Skin tones honest. Highlights occasionally rolled off. Reads like a news package",
                "colorPalette": ["#F4EAD3", "#7F8C8D", "#1F1F1F", "#D85F4A", "#FFFFFF"],
            },
            "motion": {
                "camera": "Handheld camera at chest height with subtle micro-shake. Frequent reframes as the subject reacts. Cuts to b-roll of the location. Native vertical aspect for short-form repurposing"
            },
            "references": [
                "optimistic explainer-journalist street vox",
                "broadcast-news vox pops",
                "TikTok Street Interviews",
                "between-takes interview-show cutaways",
            ],
        },
    },
    {
        "id": "bedroom-confessional",
        "label": "Bedroom Confessional",
        "description": "Intimate, low-energy direct-to-camera moment shot on a bed or floor with warm available light. Built for personal-narrative content, mental-health stories, and vulnerable creator posts.",
        "category": "influencer",
        "tags": ["influencer", "confessional", "bedroom", "personal", "intimate"],
        "promptSuffix": " Art style: Subject sits on a bed, against a headboard, or on a bedroom floor, speaking quietly to camera. Wardrobe is loungewear. Background is a real personal bedroom -- bedside table, books, soft textiles. Eye-level intimacy. Mood: Vulnerable, quiet, candid. Lighting: Warm lamp light or window light only. Soft, often low-key, with subject partially shadowed. No fill from camera. Mood preserved over flattery. Camera: Locked tripod or steady handheld at eye level. Tight medium and close-up framing. Long beats held in silence. Minimal cuts. Color grading: Warm filmic with rich shadow. Highlights softly bloomed. Skin tones warm. Looks like a private moment posted publicly. Inspired by: TikTok Confession Trend Videos, Reels Mental Health Creators, YouTube Storytime Vlogs, Vsco-Style Personal Diaries.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Vulnerable, quiet, candid",
                "artStyle": "Subject sits on a bed, against a headboard, or on a bedroom floor, speaking quietly to camera. Wardrobe is loungewear. Background is a real personal bedroom -- bedside table, books, soft textiles. Eye-level intimacy",
                "lighting": "Warm lamp light or window light only. Soft, often low-key, with subject partially shadowed. No fill from camera. Mood preserved over flattery",
                "colorGrading": "Warm filmic with rich shadow. Highlights softly bloomed. Skin tones warm. Looks like a private moment posted publicly",
                "colorPalette": ["#F4D9C2", "#A4805D", "#1F1410", "#D9A78A", "#FFFFFF"],
            },
            "motion": {
                "camera": "Locked tripod or steady handheld at eye level. Tight medium and close-up framing. Long beats held in silence. Minimal cuts"
            },
            "references": [
                "TikTok Confession Trend Videos",
                "Reels Mental Health Creators",
                "YouTube Storytime Vlogs",
                "Vsco-Style Personal Diaries",
            ],
        },
    },
    {
        "id": "kitchen-tutorial",
        "label": "Kitchen Tutorial",
        "description": "Creator-style cooking tutorial with overhead inserts, talking-head pieces-to-camera, and warm kitchen light. Built for recipe content, CPG tutorials, and food creator brand films.",
        "category": "influencer",
        "tags": ["influencer", "kitchen", "cooking", "tutorial", "recipe"],
        "promptSuffix": " Art style: Creator in a real home kitchen demonstrating a recipe. Cuts between piece-to-camera at the counter, top-down hand action, and macro detail of ingredients. Cookware honest, ingredients photographed naturally. Mood: Warm, instructive, appetizing. Lighting: Soft window daylight plus a warm overhead practical. Counter surfaces lit evenly. Bounce fill on the host’s face. Camera: Two cameras -- one locked head-and-shoulders, one locked overhead on the work surface. Macro insert lens for hand detail. Cuts driven by the recipe steps. Color grading: Warm, appetizing, with rich greens and golden tones. Skin tones warm, whites cream. Looks like a friendly recipe channel. Inspired by: newspaper test-kitchen cooking videos, editorial test-kitchen video series, high-energy chef-creator reels, warm technique-first cooking-creator cuts.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Warm, instructive, appetizing",
                "artStyle": "Creator in a real home kitchen demonstrating a recipe. Cuts between piece-to-camera at the counter, top-down hand action, and macro detail of ingredients. Cookware honest, ingredients photographed naturally",
                "lighting": "Soft window daylight plus a warm overhead practical. Counter surfaces lit evenly. Bounce fill on the host’s face",
                "colorGrading": "Warm, appetizing, with rich greens and golden tones. Skin tones warm, whites cream. Looks like a friendly recipe channel",
                "colorPalette": ["#F8EDD7", "#A18258", "#3F2A1A", "#8FA98D", "#FFFFFF"],
            },
            "motion": {
                "camera": "Two cameras -- one locked head-and-shoulders, one locked overhead on the work surface. Macro insert lens for hand detail. Cuts driven by the recipe steps"
            },
            "references": [
                "newspaper test-kitchen cooking videos",
                "editorial test-kitchen video series",
                "high-energy chef-creator reels",
                "warm technique-first cooking-creator cuts",
            ],
        },
    },
    {
        "id": "gym-selfie-cam",
        "label": "Gym Selfie Cam",
        "description": "Mirror or chest-mount gym vlog aesthetic with bright LED, sweat, and self-shot energy. Built for fitness creators, supplement brand UGC, and gymwear social cuts.",
        "category": "influencer",
        "tags": ["influencer", "gym", "fitness", "selfie", "workout"],
        "promptSuffix": " Art style: Self-shot or peer-shot gym content. Mirror selfies, rack-cam angles, GoPro on the bar, between-sets piece-to-camera. Real gym setting with rubber floor, plates, sweat, and incidental other athletes. Performance wear visible. Mood: High-energy, sweaty, motivational. Lighting: Bright commercial gym overhead LED. Mixed practicals. No diffusion. Occasional cool fluorescent kick. Camera: Phone-mounted POV, mirror selfie, chest-mounted GoPro. Mix of slow-motion lift hero shots and quick-cut piece-to-camera. Native vertical. Color grading: Cool, slightly desaturated, with high contrast. Skin slightly cool. Looks like an LED-lit gym. Inspired by: raw bodybuilding-vlog energy, aesthetic physique training reels, form-breakdown coaching demos, gymwear-brand athlete content.",
        "config": {
            "version": 2,
            "look": {
                "mood": "High-energy, sweaty, motivational",
                "artStyle": "Self-shot or peer-shot gym content. Mirror selfies, rack-cam angles, GoPro on the bar, between-sets piece-to-camera. Real gym setting with rubber floor, plates, sweat, and incidental other athletes. Performance wear visible",
                "lighting": "Bright commercial gym overhead LED. Mixed practicals. No diffusion. Occasional cool fluorescent kick",
                "colorGrading": "Cool, slightly desaturated, with high contrast. Skin slightly cool. Looks like an LED-lit gym",
                "colorPalette": ["#1A1A1A", "#FF4F1F", "#9CA3AF", "#FFFFFF", "#0A0A0A"],
            },
            "motion": {
                "camera": "Phone-mounted POV, mirror selfie, chest-mounted GoPro. Mix of slow-motion lift hero shots and quick-cut piece-to-camera. Native vertical"
            },
            "references": [
                "raw bodybuilding-vlog energy",
                "aesthetic physique training reels",
                "form-breakdown coaching demos",
                "gymwear-brand athlete content",
            ],
        },
    },
    {
        "id": "car-talk",
        "label": "Car Talk",
        "description": "In-car talking-head with dashboard mount, ambient road light, and casual confessional energy. Built for creator monologues, podcast clips, and brand storytelling in transit.",
        "category": "influencer",
        "tags": ["influencer", "car", "in-car", "talking-head", "monologue"],
        "promptSuffix": " Art style: Subject in the driver seat talking to a dashboard- or windscreen-mounted camera. Seatbelt visible, hands on the wheel. Traffic and city pass outside. Occasional cut to a side angle from the passenger seat. Mood: Casual, candid, mobile. Lighting: Available daylight through the windshield. Mixed natural and reflected light from car interior. No fill. Exposure shifts as the car moves through shadow and sun. Camera: Locked dashboard mount with subtle road jitter. Occasional cut to passenger-seat profile. Tight head-and-shoulders framing. Native vertical or 16:9. Color grading: Warm, naturalistic. Slight digital cast. Skin tones honest. Looks like real life in a car. Inspired by: celebrity carpool sing-along segments, TikTok Car Confessionals, in-car interview-show clips, Creator-Driven Monologue Reels.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Casual, candid, mobile",
                "artStyle": "Subject in the driver seat talking to a dashboard- or windscreen-mounted camera. Seatbelt visible, hands on the wheel. Traffic and city pass outside. Occasional cut to a side angle from the passenger seat",
                "lighting": "Available daylight through the windshield. Mixed natural and reflected light from car interior. No fill. Exposure shifts as the car moves through shadow and sun",
                "colorGrading": "Warm, naturalistic. Slight digital cast. Skin tones honest. Looks like real life in a car",
                "colorPalette": ["#1F1A14", "#A18258", "#F4D9C2", "#3F3F3F", "#FFFFFF"],
            },
            "motion": {
                "camera": "Locked dashboard mount with subtle road jitter. Occasional cut to passenger-seat profile. Tight head-and-shoulders framing. Native vertical or 16:9"
            },
            "references": [
                "celebrity carpool sing-along segments",
                "TikTok Car Confessionals",
                "in-car interview-show clips",
                "Creator-Driven Monologue Reels",
            ],
        },
    },
    {
        "id": "walking-and-talking",
        "label": "Walking and Talking",
        "description": "Walk-and-talk vlog energy with gimbal-stabilized motion, urban backdrop, and direct-to-camera narration. Built for creator-led brand films and city-based storytelling.",
        "category": "influencer",
        "tags": ["influencer", "walking", "gimbal", "urban", "vlog"],
        "promptSuffix": " Art style: Creator walks through a city street, park, or neighborhood while addressing the camera. Background moves behind them with parallax. Pace is brisk but conversational. Wardrobe is everyday. Mood: Energetic, exploratory, present. Lighting: Available daylight. Subject lit by sky bounce. Backlit silhouettes occasionally embraced. Exposure shifts with the environment. Camera: Gimbal-stabilized walking shot held at chest height. Subject medium-close-up with environment behind. Occasional cut to subject POV looking around. Steady but not sterile. Color grading: Warm filmic with slight contrast lift. Skin tones honest. Highlights soft. Looks like a polished creator vlog. Inspired by: kinetic city walking vlogs, polished cinematic creator vlogs, TikTok City Walks, Reels Walk-and-Talk Creators.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Energetic, exploratory, present",
                "artStyle": "Creator walks through a city street, park, or neighborhood while addressing the camera. Background moves behind them with parallax. Pace is brisk but conversational. Wardrobe is everyday",
                "lighting": "Available daylight. Subject lit by sky bounce. Backlit silhouettes occasionally embraced. Exposure shifts with the environment",
                "colorGrading": "Warm filmic with slight contrast lift. Skin tones honest. Highlights soft. Looks like a polished creator vlog",
                "colorPalette": ["#F4EAD3", "#7F8C8D", "#1F1F1F", "#D9A78A", "#FFFFFF"],
            },
            "motion": {
                "camera": "Gimbal-stabilized walking shot held at chest height. Subject medium-close-up with environment behind. Occasional cut to subject POV looking around. Steady but not sterile"
            },
            "references": [
                "kinetic city walking vlogs",
                "polished cinematic creator vlogs",
                "TikTok City Walks",
                "Reels Walk-and-Talk Creators",
            ],
        },
    },
    {
        "id": "reaction-cam",
        "label": "Reaction Cam",
        "description": "Tight reaction-style framing with small inset of the source media and exaggerated facial reaction. Built for creator reaction content, product reviews, and viral-format ads.",
        "category": "influencer",
        "tags": ["influencer", "reaction", "webcam", "review", "viral"],
        "promptSuffix": " Art style: Tight head-and-shoulders frame of a creator reacting to something off-screen. Background is a creator desk or bedroom -- LED panel, posters, gaming chair. Mic visible in foreground. Mood: Expressive, immediate, exaggerated. Lighting: RGB LED ambient on background, soft key light on the creator. Color cast deliberate -- often purples, teals, pinks. Camera: Locked webcam or desk-mounted DSLR. Tight punch-in framing. Cuts driven by reaction beats. Native vertical or 16:9. Captions burned in. Color grading: Punchy, contrasty, with vivid LED color. Skin tones warm against cool backgrounds. Looks like a streaming setup. Inspired by: high-stakes viral creator reaction cuts, Twitch Streamer Highlight Reels, TikTok React Trends, YouTube Reaction Channels.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Expressive, immediate, exaggerated",
                "artStyle": "Tight head-and-shoulders frame of a creator reacting to something off-screen. Background is a creator desk or bedroom -- LED panel, posters, gaming chair. Mic visible in foreground",
                "lighting": "RGB LED ambient on background, soft key light on the creator. Color cast deliberate -- often purples, teals, pinks",
                "colorGrading": "Punchy, contrasty, with vivid LED color. Skin tones warm against cool backgrounds. Looks like a streaming setup",
                "colorPalette": ["#1A1029", "#FF40A3", "#36E6B8", "#FFFFFF", "#0A0A0A"],
            },
            "motion": {
                "camera": "Locked webcam or desk-mounted DSLR. Tight punch-in framing. Cuts driven by reaction beats. Native vertical or 16:9. Captions burned in"
            },
            "references": [
                "high-stakes viral creator reaction cuts",
                "Twitch Streamer Highlight Reels",
                "TikTok React Trends",
                "YouTube Reaction Channels",
            ],
        },
    },
    {
        "id": "bedtime-storybook",
        "label": "Bedtime Storybook",
        "description": "Soft illustrated bedtime-story aesthetic with rounded shapes, calming colors, and a gentle narrative pace. Built for kids storytelling, lullaby content, and sleep apps.",
        "category": "kids",
        "tags": ["kids", "storybook", "bedtime", "illustration", "gentle"],
        "promptSuffix": " Art style: Hand-illustrated storybook art with rounded shapes, friendly characters, and a hand-painted texture. The scene feels like a beloved bedtime book -- soft borders, painterly backgrounds, warm faces. Not 3D, not photographic, not computer-perfect. Mood: Gentle, dreamy, reassuring. Lighting: Implied warm twilight or moonlight. Soft glows on cheeks and lanterns. Gentle gradients in sky and rooms. No harsh shadows. Camera: Static composed scenes with slow photo-pan push-ins and gentle parallax between layered illustration planes. The camera lingers. Beats long enough for a parent to point. Color grading: Warm, low-contrast, painterly. Cream highlights, dusky shadows. Looks like a beloved storybook illustration. Inspired by: classic moonlit-nursery picture-book illustrations, whimsical contemporary picture books with naive charm, rhyming woodland storybooks, chunky friendly-animal board books.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Gentle, dreamy, reassuring",
                "artStyle": "Hand-illustrated storybook art with rounded shapes, friendly characters, and a hand-painted texture. The scene feels like a beloved bedtime book -- soft borders, painterly backgrounds, warm faces. Not 3D, not photographic, not computer-perfect",
                "lighting": "Implied warm twilight or moonlight. Soft glows on cheeks and lanterns. Gentle gradients in sky and rooms. No harsh shadows",
                "colorGrading": "Warm, low-contrast, painterly. Cream highlights, dusky shadows. Looks like a beloved storybook illustration",
                "colorPalette": ["#1F2A4A", "#7F86D9", "#F6C886", "#F4A4A4", "#E8E1D0"],
            },
            "motion": {
                "camera": "Static composed scenes with slow photo-pan push-ins and gentle parallax between layered illustration planes. The camera lingers. Beats long enough for a parent to point"
            },
            "references": [
                "classic moonlit-nursery picture-book illustrations",
                "whimsical contemporary picture books with naive charm",
                "rhyming woodland storybooks",
                "chunky friendly-animal board books",
            ],
        },
    },
    {
        "id": "saturday-morning-cartoon",
        "label": "Saturday Morning Cartoon",
        "description": "Classic 90s Saturday-morning animation aesthetic with bold flat colors, snappy character animation, and zany energy. Built for kids brand films, toy launches, and animated explainers.",
        "category": "animation",
        "tags": ["kids", "cartoon", "90s", "animation", "flat-color"],
        "promptSuffix": " Art style: Flat 2D cartoon animation in the spirit of 1990s Saturday morning television. Bold color blocks, simple lineart, exaggerated character expressions, big eyes, snappy action poses. Backgrounds are simple painted plates. Not photoreal, not 3D, not CGI. Mood: Energetic, playful, zany. Lighting: Implied flat lighting with simple shadow shapes and occasional gradient skies. No volumetric or physical lighting. Camera: Smear-frame action poses, snappy holds, anticipation-and-overshoot, occasional in-camera shake on impact. Camera mostly static, characters do the work. Color grading: Saturated and punchy with crisp blacks. Looks like a TV broadcast circa 1998. Inspired by: zany 90s variety-cartoon energy, big-eyed superhero-trio cartoon styling, boy-genius laboratory cartoon geometry, rubbery undersea slapstick cartoons.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Energetic, playful, zany",
                "artStyle": "Flat 2D cartoon animation in the spirit of 1990s Saturday morning television. Bold color blocks, simple lineart, exaggerated character expressions, big eyes, snappy action poses. Backgrounds are simple painted plates. Not photoreal, not 3D, not CGI",
                "lighting": "Implied flat lighting with simple shadow shapes and occasional gradient skies. No volumetric or physical lighting",
                "colorGrading": "Saturated and punchy with crisp blacks. Looks like a TV broadcast circa 1998",
                "colorPalette": ["#FF595E", "#FFCA3A", "#8AC926", "#1982C4", "#6A4C93"],
            },
            "motion": {
                "camera": "Smear-frame action poses, snappy holds, anticipation-and-overshoot, occasional in-camera shake on impact. Camera mostly static, characters do the work"
            },
            "references": [
                "zany 90s variety-cartoon energy",
                "big-eyed superhero-trio cartoon styling",
                "boy-genius laboratory cartoon geometry",
                "rubbery undersea slapstick cartoons",
            ],
        },
    },
    {
        "id": "claymation",
        "label": "Claymation",
        "description": "Stop-motion claymation aesthetic with visible fingerprints, slight wobble, and handmade charm. Built for nostalgic kids content, craft-brand stories, and quirky narratives.",
        "category": "animation",
        "tags": ["kids", "claymation", "stop-motion", "handmade", "craft"],
        "promptSuffix": " Art style: Stop-motion clay puppet animation with visible thumbprints, subtle deformation between frames, and a slightly imperfect rig. Sets are tabletop dioramas of cardboard and clay. Characters move with quirky weight. Not CGI, not 2D animation. Mood: Whimsical, handmade, nostalgic. Lighting: Tabletop tungsten studio lighting with soft key and warm fill. Subtle shadow from each clay element on the diorama floor. Camera: Locked tripod with occasional moco moves. Slight strobing motion characteristic of 12-fps stop-motion. Cuts feel theatrical, beats held long. Color grading: Warm filmic with rich saturation and soft contrast. Slight tungsten glow. Looks like a beloved stop-motion clay short. Inspired by: inventor-and-dog clay-animation charm, plucky farmyard-escape clay features, wordless sheep-farm clay comedy, mischievous penguin-family clay shorts.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Whimsical, handmade, nostalgic",
                "artStyle": "Stop-motion clay puppet animation with visible thumbprints, subtle deformation between frames, and a slightly imperfect rig. Sets are tabletop dioramas of cardboard and clay. Characters move with quirky weight. Not CGI, not 2D animation",
                "lighting": "Tabletop tungsten studio lighting with soft key and warm fill. Subtle shadow from each clay element on the diorama floor",
                "colorGrading": "Warm filmic with rich saturation and soft contrast. Slight tungsten glow. Looks like a beloved stop-motion clay short",
                "colorPalette": ["#F4D9A8", "#C97B4E", "#3F2A1A", "#8FA98D", "#FFFFFF"],
            },
            "motion": {
                "camera": "Locked tripod with occasional moco moves. Slight strobing motion characteristic of 12-fps stop-motion. Cuts feel theatrical, beats held long"
            },
            "references": [
                "inventor-and-dog clay-animation charm",
                "plucky farmyard-escape clay features",
                "wordless sheep-farm clay comedy",
                "mischievous penguin-family clay shorts",
            ],
        },
    },
    {
        "id": "paper-cutout",
        "label": "Paper Cutout",
        "description": "Stop-motion paper-cutout aesthetic with layered card, scissor edges, and tactile shadow. Built for craft-style explainers, kids stories, and whimsical brand films.",
        "category": "animation",
        "tags": ["kids", "paper", "cutout", "collage", "craft"],
        "promptSuffix": " Art style: Stop-motion paper cutout puppets on a layered card background. Characters constructed from scissored card pieces, sometimes with visible brad joints at limbs. Sets are folded paper landscapes. Edges are deliberately imperfect. Mood: Tactile, crafty, charming. Lighting: Single soft overhead key casting clean drop shadow beneath each cutout. Slight rim from the side. Camera: Locked top-down or near-top-down camera. Slight stop-motion strobing. Pans by sliding background paper layers. Beats medium-long. Color grading: Warm naturalism with vivid paper-print color. Subtle film grain. Looks like a craft-table film. Inspired by: pioneering silhouette-animation films, crude construction-paper cutout TV comedy, Drawn-on-Film Animations, public-television counting shorts.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Tactile, crafty, charming",
                "artStyle": "Stop-motion paper cutout puppets on a layered card background. Characters constructed from scissored card pieces, sometimes with visible brad joints at limbs. Sets are folded paper landscapes. Edges are deliberately imperfect",
                "lighting": "Single soft overhead key casting clean drop shadow beneath each cutout. Slight rim from the side",
                "colorGrading": "Warm naturalism with vivid paper-print color. Subtle film grain. Looks like a craft-table film",
                "colorPalette": ["#F2EBDD", "#D85F4A", "#FFB400", "#1F8E7D", "#1F1A14"],
            },
            "motion": {
                "camera": "Locked top-down or near-top-down camera. Slight stop-motion strobing. Pans by sliding background paper layers. Beats medium-long"
            },
            "references": [
                "pioneering silhouette-animation films",
                "crude construction-paper cutout TV comedy",
                "Drawn-on-Film Animations",
                "public-television counting shorts",
            ],
        },
    },
    {
        "id": "watercolour-fable",
        "label": "Watercolour Fable",
        "description": "Hand-painted watercolour fable aesthetic with brushy edges, paper texture, and gentle narrative pace. Built for kids fables, nature stories, and read-aloud content.",
        "category": "kids",
        "tags": ["kids", "watercolour", "watercolor", "painted", "fable"],
        "promptSuffix": " Art style: Watercolor illustration on visible cotton paper. Soft washes, brush strokes, occasional pencil underdrawing, slight color bleed at edges. Characters are tender, slightly stylized, painted with a few decisive strokes. Backgrounds are atmospheric washes. Mood: Lyrical, gentle, contemplative. Lighting: Implied soft morning or twilight light. Color and tone do the work of lighting -- darker washes for shadow, paper showing through for light. Camera: Static composed scenes with very slow drifting pans and gentle parallax. Camera reverent. Long beats. Color grading: Cream paper white. Pigments rendered faithfully with slight bleed and grain. Looks like a watercolour illustration in afternoon light. Inspired by: Edwardian countryside animal-fable watercolours, mid-century lithograph picture books, pastoral anime concept watercolours, deadpan flat-wash animal illustrations.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Lyrical, gentle, contemplative",
                "artStyle": "Watercolor illustration on visible cotton paper. Soft washes, brush strokes, occasional pencil underdrawing, slight color bleed at edges. Characters are tender, slightly stylized, painted with a few decisive strokes. Backgrounds are atmospheric washes",
                "lighting": "Implied soft morning or twilight light. Color and tone do the work of lighting -- darker washes for shadow, paper showing through for light",
                "colorGrading": "Cream paper white. Pigments rendered faithfully with slight bleed and grain. Looks like a watercolour illustration in afternoon light",
                "colorPalette": ["#F2E9D8", "#7F9B7F", "#C7A1B2", "#3F4F5F", "#D8B68E"],
            },
            "motion": {
                "camera": "Static composed scenes with very slow drifting pans and gentle parallax. Camera reverent. Long beats"
            },
            "references": [
                "Edwardian countryside animal-fable watercolours",
                "mid-century lithograph picture books",
                "pastoral anime concept watercolours",
                "deadpan flat-wash animal illustrations",
            ],
        },
    },
    {
        "id": "pop-up-book",
        "label": "Pop-Up Book",
        "description": "Mechanical pop-up book aesthetic with hinged paper, perspective reveals, and tactile depth. Built for kids storytelling, branded books-in-motion, and educational reveals.",
        "category": "kids",
        "tags": ["kids", "pop-up", "paper", "mechanical", "storybook"],
        "promptSuffix": " Art style: The scene built as a layered papercraft diorama -- characters, trees, and props as scissored, hand-cut paper standing at staggered depths, soft folds and cut edges catching light, against a patterned cut-paper backdrop. A dimensional cut-paper world filling the entire frame. Mood: Discovery, magical, tactile. Lighting: Warm, soft directional light with gentle shadows falling between the layered paper planes. Highlights catch the cut-paper edges. Cozy, intimate glow. Camera: Slow push-in revealing the layered paper depth. Gentle drift across the diorama to discover detail. Beats held to admire each cut-paper layer. Color grading: Warm, handcrafted grade. Cream highlights, soft shadow between the paper layers. Rich, tactile cut-paper warmth. Inspired by: classic silhouette-animation films, cut-paper stop-motion animation, layered papercraft dioramas, hand-cut paper illustration.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Discovery, magical, tactile",
                "artStyle": "The scene built as a layered papercraft diorama -- characters, trees, and props as scissored, hand-cut paper standing at staggered depths, soft folds and cut edges catching light, against a patterned cut-paper backdrop. A dimensional cut-paper world filling the entire frame",
                "lighting": "Warm, soft directional light with gentle shadows falling between the layered paper planes. Highlights catch the cut-paper edges. Cozy, intimate glow",
                "colorGrading": "Warm, handcrafted grade. Cream highlights, soft shadow between the paper layers. Rich, tactile cut-paper warmth",
                "colorPalette": ["#F4E4CF", "#C97B4E", "#1F8E7D", "#D85F4A", "#FFFFFF"],
            },
            "motion": {
                "camera": "Slow push-in revealing the layered paper depth. Gentle drift across the diorama to discover detail. Beats held to admire each cut-paper layer"
            },
            "references": [
                "classic silhouette-animation films",
                "cut-paper stop-motion animation",
                "layered papercraft dioramas",
                "hand-cut paper illustration",
            ],
        },
    },
    {
        "id": "felt-and-yarn",
        "label": "Felt and Yarn",
        "description": "Stop-motion felt and yarn puppet aesthetic with soft textiles, button eyes, and homespun charm. Built for cozy kids stories and craft-brand films.",
        "category": "animation",
        "tags": ["kids", "felt", "yarn", "puppet", "stop-motion"],
        "promptSuffix": " Art style: Stop-motion felt and yarn puppets with button eyes and hand-stitched mouths. Sets built from textured fabric, knitted backdrops, and felt scenery. Characters move with handmade weight, visible thread and seams celebrated. Mood: Soft, cozy, homespun. Lighting: Soft tabletop key with warm fill. Subtle shadow on textured fabric. Highlights catch fluff and fiber. Camera: Locked tripod with subtle stop-motion strobing. Slow pans across stitched landscapes. Beats long enough to feel the wool. Color grading: Warm filmic with rich textile color. Slight grain. Looks like a beloved hand-knit world. Inspired by: whistling knitted-space-creature stop-motion, cloth-cat curiosity-shop puppet films, soft twilight puppet-garden television, felt-puppet preschool storytelling.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Soft, cozy, homespun",
                "artStyle": "Stop-motion felt and yarn puppets with button eyes and hand-stitched mouths. Sets built from textured fabric, knitted backdrops, and felt scenery. Characters move with handmade weight, visible thread and seams celebrated",
                "lighting": "Soft tabletop key with warm fill. Subtle shadow on textured fabric. Highlights catch fluff and fiber",
                "colorGrading": "Warm filmic with rich textile color. Slight grain. Looks like a beloved hand-knit world",
                "colorPalette": ["#F4E4CF", "#D8B68E", "#7F9B7F", "#D85F4A", "#FFFFFF"],
            },
            "motion": {
                "camera": "Locked tripod with subtle stop-motion strobing. Slow pans across stitched landscapes. Beats long enough to feel the wool"
            },
            "references": [
                "whistling knitted-space-creature stop-motion",
                "cloth-cat curiosity-shop puppet films",
                "soft twilight puppet-garden television",
                "felt-puppet preschool storytelling",
            ],
        },
    },
    {
        "id": "chalkboard-doodle",
        "label": "Chalkboard Doodle",
        "description": "Hand-drawn chalkboard animation aesthetic with white chalk on slate, erasure smudges, and live drawing energy. Built for educational shorts, kids explainers, and STEM stories.",
        "category": "animation",
        "tags": ["kids", "chalkboard", "doodle", "educational", "hand-drawn"],
        "promptSuffix": " Art style: White and pastel chalk drawn on a dark green or black slate. Lines accumulate as the narrator describes. Diagrams, doodles, and characters appear and erase. Visible smudges and chalk dust. Not slick, not vector. Mood: Inviting, instructive, hand-drawn. Lighting: Even ambient lighting on the chalkboard surface. Slight rake to bring out chalk texture. No drama, just legibility. Camera: Mostly locked frame on the chalk-drawn scene with occasional push-in to highlight a diagram. Beats paced for narration. Color grading: Slate green or near-black background, pastel chalk colors rendered with slight glow. Looks like a chalk drawing on a sunlit slate surface. Inspired by: animated explainer-lesson shorts, live-drawn lecture-illustration talks, colorful science-explainer whiteboard variants, digital blackboard tutorial doodles.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Inviting, instructive, hand-drawn",
                "artStyle": "White and pastel chalk drawn on a dark green or black slate. Lines accumulate as the narrator describes. Diagrams, doodles, and characters appear and erase. Visible smudges and chalk dust. Not slick, not vector",
                "lighting": "Even ambient lighting on the chalkboard surface. Slight rake to bring out chalk texture. No drama, just legibility",
                "colorGrading": "Slate green or near-black background, pastel chalk colors rendered with slight glow. Looks like a chalk drawing on a sunlit slate surface",
                "colorPalette": ["#1F2A1F", "#F8F4E3", "#F4C065", "#A8DFFF", "#FF8FA3"],
            },
            "motion": {
                "camera": "Mostly locked frame on the chalk-drawn scene with occasional push-in to highlight a diagram. Beats paced for narration"
            },
            "references": [
                "animated explainer-lesson shorts",
                "live-drawn lecture-illustration talks",
                "colorful science-explainer whiteboard variants",
                "digital blackboard tutorial doodles",
            ],
        },
    },
    {
        "id": "lo-fi-anime",
        "label": "Lo-Fi Anime",
        "description": "Soft 90s-anime aesthetic with cel-shaded characters, painted backgrounds, and contemplative pacing. Built for older kids and teen storytelling, music videos, and slice-of-life narratives.",
        "category": "animation",
        "tags": ["kids", "anime", "lo-fi", "cel-shaded", "slice-of-life"],
        "promptSuffix": " Art style: Hand-drawn cel-shaded anime in the spirit of late 1990s television -- two-tone shading on characters, painted gouache backgrounds, sparse keyframes between long held beats. Subject is often a teen in a quiet domestic moment -- a window seat, a train ride, a corner store. Mood: Wistful, slow, atmospheric. Lighting: Warm afternoon or magic-hour light in the painted backgrounds. Cel shading on characters in two clean tones. Atmospheric haze in distant elements. Camera: Long held compositions with subtle parallax pans. Slow zooms into character eyes for emotional beats. Cuts feel weighted, breaths between dialogue. Color grading: Warm, slightly faded, low-contrast. Looks like a VHS pull from a beloved tape. Inspired by: painterly pastoral anime features, light-drenched luminous-sky anime shorts, jazz-scored space-western anime quiet scenes, Lo-Fi Hip-Hop Radio Loop Backgrounds.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Wistful, slow, atmospheric",
                "artStyle": "Hand-drawn cel-shaded anime in the spirit of late 1990s television -- two-tone shading on characters, painted gouache backgrounds, sparse keyframes between long held beats. Subject is often a teen in a quiet domestic moment -- a window seat, a train ride, a corner store",
                "lighting": "Warm afternoon or magic-hour light in the painted backgrounds. Cel shading on characters in two clean tones. Atmospheric haze in distant elements",
                "colorGrading": "Warm, slightly faded, low-contrast. Looks like a VHS pull from a beloved tape",
                "colorPalette": ["#F4D9A8", "#E69B6F", "#7F86D9", "#1F2A4A", "#F8F4E3"],
            },
            "motion": {
                "camera": "Long held compositions with subtle parallax pans. Slow zooms into character eyes for emotional beats. Cuts feel weighted, breaths between dialogue"
            },
            "references": [
                "painterly pastoral anime features",
                "light-drenched luminous-sky anime shorts",
                "jazz-scored space-western anime quiet scenes",
                "Lo-Fi Hip-Hop Radio Loop Backgrounds",
            ],
        },
    },
    {
        "id": "hand-drawn-picture-book",
        "label": "Hand-Drawn Picture Book",
        "description": "Crayon and colored-pencil picture-book aesthetic with playful linework and bright fields of color. Built for early-reader content and kids brand storytelling.",
        "category": "kids",
        "tags": ["kids", "picture-book", "crayon", "pencil", "illustration"],
        "promptSuffix": " Art style: Hand-drawn picture book illustration with crayon and colored pencil texture. Loose linework, fields of bright flat color, occasional scribble texture, simplified figures with big friendly faces. Backgrounds spare but expressive. Mood: Playful, bright, child-led. Lighting: Implied flat illustration light. No physical light model. Tone and color do all the work. Camera: Static composed framing with slow push-ins and gentle parallax between drawn layers. Camera respectful of the artwork. Color grading: Bright, saturated, with paper-cream background and slight crayon grain. Warm, freshly-printed picture-book feel. Inspired by: painted-tissue collage picture books, expressive minimal-line cartoon picture books, surreal painterly picture-book illustrations, classic European picture stories.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Playful, bright, child-led",
                "artStyle": "Hand-drawn picture book illustration with crayon and colored pencil texture. Loose linework, fields of bright flat color, occasional scribble texture, simplified figures with big friendly faces. Backgrounds spare but expressive",
                "lighting": "Implied flat illustration light. No physical light model. Tone and color do all the work",
                "colorGrading": "Bright, saturated, with paper-cream background and slight crayon grain. Warm, freshly-printed picture-book feel",
                "colorPalette": ["#FFCB47", "#FF6B6B", "#4ECDC4", "#1F2A4A", "#F8F4E3"],
            },
            "motion": {
                "camera": "Static composed framing with slow push-ins and gentle parallax between drawn layers. Camera respectful of the artwork"
            },
            "references": [
                "painted-tissue collage picture books",
                "expressive minimal-line cartoon picture books",
                "surreal painterly picture-book illustrations",
                "classic European picture stories",
            ],
        },
    },
    {
        "id": "rough-storyboard",
        "label": "Rough Storyboard",
        "description": "Pencil-and-marker storyboard panel aesthetic for pre-production and pitch reels. Built for animatic explorations and director treatments.",
        "category": "animatic",
        "tags": ["animatic", "storyboard", "pre-vis", "pencil", "pitch"],
        "promptSuffix": " Art style: A single film frame rendered as a rough pencil-and-marker storyboard sketch. Confident gestural lines, simplified silhouettes, light marker shading. Faces and hands described with a few decisive strokes -- expressive but not finished. One full-bleed drawn frame depicting the scene -- no panel grid, no frame numbers, no margin notes. Mood: Working-draft, exploratory, gestural. Lighting: Implied value-shading only. Hatching and marker wash suggest light direction. No rendered lighting. Camera: Static, clearly-staged composition -- a single readable wide, medium, or close. Minimal camera movement. No drawn arrows, nested frames, or move-diagrams in the image -- one staged frame. Color grading: Warm paper-tone background with occasional smudge. Marker-bleed mid-tones, cool shadow ink. Looks like a rough hand-drawn sketch. Inspired by: feature-animation storyboard reels, studio animatic test reels, painterly hand-drawn imageboards, 90s hand-drawn feature pre-vis reels.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Working-draft, exploratory, gestural",
                "artStyle": "A single film frame rendered as a rough pencil-and-marker storyboard sketch. Confident gestural lines, simplified silhouettes, light marker shading. Faces and hands described with a few decisive strokes -- expressive but not finished. One full-bleed drawn frame depicting the scene -- no panel grid, no frame numbers, no margin notes",
                "lighting": "Implied value-shading only. Hatching and marker wash suggest light direction. No rendered lighting",
                "colorGrading": "Warm paper-tone background with occasional smudge. Marker-bleed mid-tones, cool shadow ink. Looks like a rough hand-drawn sketch",
                "colorPalette": ["#F5F1E8", "#2B2826", "#7A6F66", "#C44536", "#3A6B8C"],
            },
            "motion": {
                "camera": "Static, clearly-staged composition -- a single readable wide, medium, or close. Minimal camera movement. No drawn arrows, nested frames, or move-diagrams in the image -- one staged frame"
            },
            "references": [
                "feature-animation storyboard reels",
                "studio animatic test reels",
                "painterly hand-drawn imageboards",
                "90s hand-drawn feature pre-vis reels",
            ],
        },
    },
    {
        "id": "low-detail-blockout",
        "label": "Low-Detail Blockout",
        "description": "Untextured 3D blockout aesthetic for early staging, layout, and shot composition. Built for previs reels, animation layout reviews, and architectural concept passes.",
        "category": "animatic",
        "tags": ["animatic", "blockout", "3d", "previs", "layout"],
        "promptSuffix": " Art style: Untextured 3D greybox blockout. Characters are featureless low-poly grey mannequins -- smooth blocky primitives with no faces, no fingers, no surface detail. Props and environment are simple box geometry with minimal detail. Visible polygon edges, flat ambient shading only. A few clearly-staged figures, never a dense crowd. No materials, no textures. Mood: Functional, schematic, in-progress. Lighting: Flat ambient occlusion lighting only. No direct light source, no shadow drama. Functional visibility. Camera: A single wide layout camera with a slow tracking move to verify staging. One clearly-staged composition, no burned-in focal-length or shot-number annotations. Color grading: Flat, neutral, with no stylization. Looks like a raw OpenGL viewport. Inspired by: feature-animation layout reels, studio animation previs, game-engine greybox previews, Game Cinematic Blockouts.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Functional, schematic, in-progress",
                "artStyle": "Untextured 3D greybox blockout. Characters are featureless low-poly grey mannequins -- smooth blocky primitives with no faces, no fingers, no surface detail. Props and environment are simple box geometry with minimal detail. Visible polygon edges, flat ambient shading only. A few clearly-staged figures, never a dense crowd. No materials, no textures",
                "lighting": "Flat ambient occlusion lighting only. No direct light source, no shadow drama. Functional visibility",
                "colorGrading": "Flat, neutral, with no stylization. Looks like a raw OpenGL viewport",
                "colorPalette": ["#B0B0B0", "#7F7F7F", "#4F4F4F", "#3478F6", "#FFFFFF"],
            },
            "motion": {
                "camera": "A single wide layout camera with a slow tracking move to verify staging. One clearly-staged composition, no burned-in focal-length or shot-number annotations"
            },
            "references": [
                "feature-animation layout reels",
                "studio animation previs",
                "game-engine greybox previews",
                "Game Cinematic Blockouts",
            ],
        },
    },
    {
        "id": "mood-only-frames",
        "label": "Mood-Only Frames",
        "description": "Atmospheric mood-frame aesthetic for early treatments and pitch documents -- color, light, and tone over plot. Built for director lookbooks and brand-film treatments.",
        "category": "animatic",
        "tags": ["animatic", "mood", "treatment", "lookbook", "pitch"],
        "promptSuffix": " Art style: Single hero moments rendered like still-frame paintings -- a silhouette in fog, a hand on a window, a sun pouring through curtains. Plot is implied, never spelled out. Subject ambiguous, color and light dominant. Mood: Suggestive, atmospheric, painterly. Lighting: Highly designed cinematic lighting -- single hard key with deep falloff, or saturated practical sources. Every frame considered as a lighting study. Camera: A locked static composition held in stillness. Slow push-in on the key moment. No camera movement that would distract from the composition. Color grading: Bold, saturated, painterly. Strong designed color palette. Cinematic black levels. Inspired by: master-cinematographer lighting stills, natural-light long-take frame studies, saturated neon-romance color lookbooks, humid slow-cinema stills.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Suggestive, atmospheric, painterly",
                "artStyle": "Single hero moments rendered like still-frame paintings -- a silhouette in fog, a hand on a window, a sun pouring through curtains. Plot is implied, never spelled out. Subject ambiguous, color and light dominant",
                "lighting": "Highly designed cinematic lighting -- single hard key with deep falloff, or saturated practical sources. Every frame considered as a lighting study",
                "colorGrading": "Bold, saturated, painterly. Strong designed color palette. Cinematic black levels",
                "colorPalette": ["#1F2A40", "#A9633C", "#E8C28C", "#3F3F3F", "#F4E4CF"],
            },
            "motion": {
                "camera": "A locked static composition held in stillness. Slow push-in on the key moment. No camera movement that would distract from the composition"
            },
            "references": [
                "master-cinematographer lighting stills",
                "natural-light long-take frame studies",
                "saturated neon-romance color lookbooks",
                "humid slow-cinema stills",
            ],
        },
    },
    {
        "id": "black-white-previz",
        "label": "Black & White Previz",
        "description": "Monochrome staging passes with full lighting and composition but no color information. Built for shot-design review and pre-production decisions where color would distract.",
        "category": "animatic",
        "tags": ["animatic", "bw", "previz", "monochrome", "staging"],
        "promptSuffix": " Art style: Full-scene previz rendered in monochrome -- characters, environments, and lighting present, color stripped. Composition, staging, and silhouette tested in pure tone. Sometimes a single hero accent color permitted. Mood: Disciplined, compositional, focused. Lighting: Real cinematic lighting setups -- hard key, soft fill, rim. Shadow shapes are the protagonists. Volumetric haze allowed. Camera: Real production cinematography -- handheld, dolly, crane. Camera moves at intended final pacing. No burned-in shot numbers or lens annotations. Color grading: Classic monochrome with rich tonal range. Deep blacks, luminous whites, controlled midtones. Looks like a black-and-white film. Inspired by: stark wartime monochrome drama stills, storm-lashed monochrome maritime madness, monochrome memory-film pre-production reels, gritty character-study monochrome grades.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Disciplined, compositional, focused",
                "artStyle": "Full-scene previz rendered in monochrome -- characters, environments, and lighting present, color stripped. Composition, staging, and silhouette tested in pure tone. Sometimes a single hero accent color permitted",
                "lighting": "Real cinematic lighting setups -- hard key, soft fill, rim. Shadow shapes are the protagonists. Volumetric haze allowed",
                "colorGrading": "Classic monochrome with rich tonal range. Deep blacks, luminous whites, controlled midtones. Looks like a black-and-white film",
                "colorPalette": ["#0A0A0A", "#3F3F3F", "#7F7F7F", "#C0C0C0", "#FFFFFF"],
            },
            "motion": {
                "camera": "Real production cinematography -- handheld, dolly, crane. Camera moves at intended final pacing. No burned-in shot numbers or lens annotations"
            },
            "references": [
                "stark wartime monochrome drama stills",
                "storm-lashed monochrome maritime madness",
                "monochrome memory-film pre-production reels",
                "gritty character-study monochrome grades",
            ],
        },
    },
    {
        "id": "schematic-concept",
        "label": "Schematic Concept",
        "description": "Diagrammatic illustration aesthetic with labels, callouts, and isometric views. Built for technical pitches, product concept docs, and internal explainers.",
        "category": "animatic",
        "tags": ["animatic", "schematic", "diagram", "isometric", "concept"],
        "promptSuffix": " Art style: Diagrammatic concept illustration. Isometric or exploded views of products, environments, or systems. Clean linework, labels with leader lines, dashed bounding boxes for callouts. Color used sparingly to highlight focus. Mood: Technical, clear, considered. Lighting: Flat technical illustration light. No physical lighting model. Subtle gradient fills for surface. Camera: Static isometric or perspective framing. Slow zoom into a callout area to draw focus. Camera holds steady on the single diagram. Color grading: Clean, neutral, with crisp linework. Slight paper-grain background. Looks like a polished technical doc. Inspired by: consumer-electronics patent drawings, magazine how-it-works diagrams, flat-pack assembly instructions, space-agency technical concept boards.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Technical, clear, considered",
                "artStyle": "Diagrammatic concept illustration. Isometric or exploded views of products, environments, or systems. Clean linework, labels with leader lines, dashed bounding boxes for callouts. Color used sparingly to highlight focus",
                "lighting": "Flat technical illustration light. No physical lighting model. Subtle gradient fills for surface",
                "colorGrading": "Clean, neutral, with crisp linework. Slight paper-grain background. Looks like a polished technical doc",
                "colorPalette": ["#F7F8FB", "#0A2540", "#3478F6", "#F4C065", "#A8AFB8"],
            },
            "motion": {
                "camera": "Static isometric or perspective framing. Slow zoom into a callout area to draw focus. Camera holds steady on the single diagram"
            },
            "references": [
                "consumer-electronics patent drawings",
                "magazine how-it-works diagrams",
                "flat-pack assembly instructions",
                "space-agency technical concept boards",
            ],
        },
    },
    {
        "id": "marker-rough",
        "label": "Marker Rough",
        "description": "Loose marker-comp aesthetic with bold flats, drop shadows, and ad-agency layout energy. Built for creative pitches, ad concepts, and storyboards mid-comp.",
        "category": "animatic",
        "tags": ["animatic", "marker", "comp", "ad-concept", "pitch"],
        "promptSuffix": " Art style: A single frame rendered in the style of a marker comp -- bold flat marker tones with one or two accent colors, soft drop shadow under figures, simplified silhouettes. The energy of an art director’s pitch art applied to one full-bleed frame depicting the scene -- no layout-sheet borders, headlines, or callouts. Mood: Loose, energetic, ad-agency. Lighting: Implied flat marker shading. Simple value flats with a single highlight per object. Camera: Static, composed framing with a subtle drifting pan. Beats sized for narration. A single full-bleed composition -- not a comp page or board carrying multiple thumbnails. Color grading: Warm paper background with vivid marker color. Slight grain. Looks like fresh marker on paper. Inspired by: 1960s ad-agency pitch boards, global ad-agency concept reels, agency internal concept decks, creative-agency pitch boards.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Loose, energetic, ad-agency",
                "artStyle": "A single frame rendered in the style of a marker comp -- bold flat marker tones with one or two accent colors, soft drop shadow under figures, simplified silhouettes. The energy of an art director’s pitch art applied to one full-bleed frame depicting the scene -- no layout-sheet borders, headlines, or callouts",
                "lighting": "Implied flat marker shading. Simple value flats with a single highlight per object",
                "colorGrading": "Warm paper background with vivid marker color. Slight grain. Looks like fresh marker on paper",
                "colorPalette": ["#F5F1E8", "#2B2826", "#FFCA3A", "#FF595E", "#1982C4"],
            },
            "motion": {
                "camera": "Static, composed framing with a subtle drifting pan. Beats sized for narration. A single full-bleed composition -- not a comp page or board carrying multiple thumbnails"
            },
            "references": [
                "1960s ad-agency pitch boards",
                "global ad-agency concept reels",
                "agency internal concept decks",
                "creative-agency pitch boards",
            ],
        },
    },
    {
        "id": "greyscale-layout",
        "label": "Greyscale Layout",
        "description": "Greyscale shot-layout aesthetic with tonal blockouts of full scenes -- no color, no detail. Built for shot-design review and director walkthroughs.",
        "category": "animatic",
        "tags": ["animatic", "greyscale", "layout", "tonal", "review"],
        "promptSuffix": " Art style: Full-scene compositions painted in greyscale with five or six tonal values. Characters in silhouette against environments built from blocked-in light and shadow. Composition prioritized over detail. Mood: Analytical, in-progress, considered. Lighting: Implied via tonal value. Strong key direction visible in the shadow shapes. No texture, no specular. Camera: Static or slow drift compositions. Frames feel like paintings. Camera moves are reserved for hero moments. Color grading: Monochrome with rich tonal range. Looks like a pre-color-script frame. Inspired by: feature-animation visual-development reels, studio concept paintings, Animation Master Class Tonal Studies, animation color-script drafts.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Analytical, in-progress, considered",
                "artStyle": "Full-scene compositions painted in greyscale with five or six tonal values. Characters in silhouette against environments built from blocked-in light and shadow. Composition prioritized over detail",
                "lighting": "Implied via tonal value. Strong key direction visible in the shadow shapes. No texture, no specular",
                "colorGrading": "Monochrome with rich tonal range. Looks like a pre-color-script frame",
                "colorPalette": ["#0A0A0A", "#3F3F3F", "#7F7F7F", "#C0C0C0", "#FFFFFF"],
            },
            "motion": {
                "camera": "Static or slow drift compositions. Frames feel like paintings. Camera moves are reserved for hero moments"
            },
            "references": [
                "feature-animation visual-development reels",
                "studio concept paintings",
                "Animation Master Class Tonal Studies",
                "animation color-script drafts",
            ],
        },
    },
    {
        "id": "intentionally-unfinished",
        "label": "Intentionally Unfinished",
        "description": "Mixed-fidelity pitch aesthetic that combines polished hero frames with sketch panels and notes -- showing what the final could be next to the working draft. Built for treatments and internal reviews.",
        "category": "animatic",
        "tags": ["animatic", "mixed-fidelity", "unfinished", "treatment", "pitch"],
        "promptSuffix": " Art style: A single frame that looks like a deliberate work-in-progress -- a largely cinematic shot with patches left as rough sketch, light annotation scribbles, and partially-rendered areas, celebrating the working-draft look. One frame depicting the scene, not a reel or montage of mixed media. Mood: Honest, exploratory, in-progress. Lighting: Mixed within the one frame -- finished areas fully lit cinematically, rough areas implied tonally, annotation scribbles flat and graphic. Camera: A single composed shot at intended pacing, with sketch-overlay passes and partial renders visible within the one frame. Not a multi-shot reel or jump-cut montage. Color grading: Mixed within the one frame -- finished areas fully graded, rough areas paper-tone, notes flat. The contrast is intentional. Inspired by: Director Treatments, playful auteur pitch reels, indie-studio production lookbooks, nostalgic analog-texture visual treatments.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Honest, exploratory, in-progress",
                "artStyle": "A single frame that looks like a deliberate work-in-progress -- a largely cinematic shot with patches left as rough sketch, light annotation scribbles, and partially-rendered areas, celebrating the working-draft look. One frame depicting the scene, not a reel or montage of mixed media",
                "lighting": "Mixed within the one frame -- finished areas fully lit cinematically, rough areas implied tonally, annotation scribbles flat and graphic",
                "colorGrading": "Mixed within the one frame -- finished areas fully graded, rough areas paper-tone, notes flat. The contrast is intentional",
                "colorPalette": ["#F5F1E8", "#2B2826", "#7A6F66", "#C44536", "#FFFFFF"],
            },
            "motion": {
                "camera": "A single composed shot at intended pacing, with sketch-overlay passes and partial renders visible within the one frame. Not a multi-shot reel or jump-cut montage"
            },
            "references": [
                "Director Treatments",
                "playful auteur pitch reels",
                "indie-studio production lookbooks",
                "nostalgic analog-texture visual treatments",
            ],
        },
    },
    {
        "id": "real-estate-listing",
        "label": "Real Estate Listing",
        "description": "Bright, wide-angle real estate listing aesthetic with even daylight, level horizons, and architectural clarity. Built for residential PDPs, agent reels, and broker hero films.",
        "category": "commercial",
        "tags": ["realestate", "listing", "residential", "agent", "tour"],
        "promptSuffix": " Art style: Real estate listing cinematography. Wide-angle interior frames showing room layout, exterior establishing shots at midday, drone reveal of property from the air. Furnishings styled but not over-styled. Vertical and horizontal lines perfectly level. Mood: Bright, welcoming, spacious. Lighting: Bright even daylight through windows balanced with HDR interior exposure. Lamps and ceiling lights on. Shadows soft, highlights controlled. Everything legible. Camera: Slow gimbal walks through rooms, drone reveal exteriors, locked wide interior frames. Level horizons, plumb verticals. Cuts paced for walkthrough rhythm. Color grading: Bright and clean with neutral whites and slightly warm woods. Greens in landscaping enhanced. Looks like a magazine real estate spread. Inspired by: Zillow 3D Tours, modern brokerage brand films, Redfin Listing Videos, luxury brokerage property films.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Bright, welcoming, spacious",
                "artStyle": "Real estate listing cinematography. Wide-angle interior frames showing room layout, exterior establishing shots at midday, drone reveal of property from the air. Furnishings styled but not over-styled. Vertical and horizontal lines perfectly level",
                "lighting": "Bright even daylight through windows balanced with HDR interior exposure. Lamps and ceiling lights on. Shadows soft, highlights controlled. Everything legible",
                "colorGrading": "Bright and clean with neutral whites and slightly warm woods. Greens in landscaping enhanced. Looks like a magazine real estate spread",
                "colorPalette": ["#F8F4ED", "#A18258", "#3F3F3F", "#8FA98D", "#FFFFFF"],
            },
            "motion": {
                "camera": "Slow gimbal walks through rooms, drone reveal exteriors, locked wide interior frames. Level horizons, plumb verticals. Cuts paced for walkthrough rhythm"
            },
            "references": [
                "Zillow 3D Tours",
                "modern brokerage brand films",
                "Redfin Listing Videos",
                "luxury brokerage property films",
            ],
        },
    },
    {
        "id": "saas-product-demo",
        "label": "SaaS Product Demo",
        "description": "Screen-recording-meets-narrator SaaS demo aesthetic with crisp UI capture, subtle motion, and confident narration cuts. Built for product launches, onboarding, and feature explainers.",
        "category": "tech",
        "tags": ["saas", "demo", "product", "ui", "explainer"],
        "promptSuffix": " Art style: High-fidelity SaaS product demo. The application UI fills most of the frame, cursor moves with intention through workflows, key elements zoom in with annotations. Cuts to a narrator at a clean desk or to a customer in context. Mockup devices framed in product hands. Mood: Confident, clear, modern. Lighting: For UI captures, pixel-perfect screen renders. For human moments, soft natural daylight at a desk. Clean, modern, never moody. Camera: Locked UI captures with smooth cursor motion, subtle push-in zooms on key features. Cuts to medium head-and-shoulders of narrator. B-roll inserts of teammates at laptops. Color grading: Bright and clean. UI rendered crisply. Skin tones natural. One brand accent color punches. Looks like a launch-day landing page. Inspired by: crafted dev-tool product films, productivity-app launch videos, design-tool conference spots, developer-platform product reveals.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Confident, clear, modern",
                "artStyle": "High-fidelity SaaS product demo. The application UI fills most of the frame, cursor moves with intention through workflows, key elements zoom in with annotations. Cuts to a narrator at a clean desk or to a customer in context. Mockup devices framed in product hands",
                "lighting": "For UI captures, pixel-perfect screen renders. For human moments, soft natural daylight at a desk. Clean, modern, never moody",
                "colorGrading": "Bright and clean. UI rendered crisply. Skin tones natural. One brand accent color punches. Looks like a launch-day landing page",
                "colorPalette": ["#F7F8FB", "#0A2540", "#3478F6", "#22C55E", "#FFFFFF"],
            },
            "motion": {
                "camera": "Locked UI captures with smooth cursor motion, subtle push-in zooms on key features. Cuts to medium head-and-shoulders of narrator. B-roll inserts of teammates at laptops"
            },
            "references": [
                "crafted dev-tool product films",
                "productivity-app launch videos",
                "design-tool conference spots",
                "developer-platform product reveals",
            ],
        },
    },
    {
        "id": "healthcare-patient-story",
        "label": "Healthcare Patient Story",
        "description": "Warm, documentary-style patient-story aesthetic with soft natural light, honest faces, and unhurried pace. Built for healthcare brand films, fundraising stories, and clinical case studies.",
        "category": "commercial",
        "tags": ["healthcare", "patient", "documentary", "story", "brand"],
        "promptSuffix": " Art style: Documentary-style patient and family portraits. Subjects at home, in clinic, or in everyday moments -- a morning coffee, a walk in the garden, a hand held by a clinician. Real environments, modest art direction, no styling that hides the humanity. Mood: Warm, dignified, hopeful. Lighting: Soft natural daylight through windows with subtle reflector fill. Practicals at evening. Faces always flattering, never clinical. Highlights gentle. Camera: Light handheld for interview and observation. Locked frames for portraits. Macro detail for tactile moments -- hand on hand, glass of water, photograph on a shelf. Color grading: Warm, soft, with gentle saturation. Skin tones honest, shadows luminous. Looks like a respectful documentary. Inspired by: academic medical-center brand films, hospital patient-story films, health-system brand spots, Pediatric Hospital Brand Reels.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Warm, dignified, hopeful",
                "artStyle": "Documentary-style patient and family portraits. Subjects at home, in clinic, or in everyday moments -- a morning coffee, a walk in the garden, a hand held by a clinician. Real environments, modest art direction, no styling that hides the humanity",
                "lighting": "Soft natural daylight through windows with subtle reflector fill. Practicals at evening. Faces always flattering, never clinical. Highlights gentle",
                "colorGrading": "Warm, soft, with gentle saturation. Skin tones honest, shadows luminous. Looks like a respectful documentary",
                "colorPalette": ["#F4E4CF", "#A8C0B5", "#3F3F3F", "#D9A78A", "#FFFFFF"],
            },
            "motion": {
                "camera": "Light handheld for interview and observation. Locked frames for portraits. Macro detail for tactile moments -- hand on hand, glass of water, photograph on a shelf"
            },
            "references": [
                "academic medical-center brand films",
                "hospital patient-story films",
                "health-system brand spots",
                "Pediatric Hospital Brand Reels",
            ],
        },
    },
    {
        "id": "restaurant-menu-hero",
        "label": "Restaurant Menu Hero",
        "description": "Appetizing restaurant menu and signature-dish cinematography. Built for restaurant brand films, delivery app menus, and seasonal menu launches.",
        "category": "commercial",
        "tags": ["restaurant", "menu", "food", "dish", "hero"],
        "promptSuffix": " Art style: Restaurant hero food photography. Each dish photographed at the moment of plating -- garnish placed, sauce drizzled, herbs scattered. Surfaces are restaurant-honest -- raw wood, slate, marble. Cutlery and napkins styled simply. Chef hands occasionally enter frame. Mood: Appetizing, crafted, inviting. Lighting: Soft directional window light or designed restaurant practicals. Strong backlight to make steam, sheen, and oil glow. Subtle fill keeps shadows luminous. Camera: Overhead flat-lay reveals, slow tabletop tracks past plated dishes, macro detail of sauces and garnishes. Beats held to feel the texture. Color grading: Warm and rich, with vivid greens, golden sauces, deep walnut shadows. Skin tones (if visible) warm. Looks like a restaurant cookbook spread. Inspired by: fine-dining tasting-room brand films, new-nordic restaurant documentary cuts, Doordash Menu Hero Spots, sensory food-science series episodes.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Appetizing, crafted, inviting",
                "artStyle": "Restaurant hero food photography. Each dish photographed at the moment of plating -- garnish placed, sauce drizzled, herbs scattered. Surfaces are restaurant-honest -- raw wood, slate, marble. Cutlery and napkins styled simply. Chef hands occasionally enter frame",
                "lighting": "Soft directional window light or designed restaurant practicals. Strong backlight to make steam, sheen, and oil glow. Subtle fill keeps shadows luminous",
                "colorGrading": "Warm and rich, with vivid greens, golden sauces, deep walnut shadows. Skin tones (if visible) warm. Looks like a restaurant cookbook spread",
                "colorPalette": ["#3F2A1A", "#C97B4E", "#F0D7A1", "#1F2F1F", "#FFFFFF"],
            },
            "motion": {
                "camera": "Overhead flat-lay reveals, slow tabletop tracks past plated dishes, macro detail of sauces and garnishes. Beats held to feel the texture"
            },
            "references": [
                "fine-dining tasting-room brand films",
                "new-nordic restaurant documentary cuts",
                "Doordash Menu Hero Spots",
                "sensory food-science series episodes",
            ],
        },
    },
    {
        "id": "fitness-coaching",
        "label": "Fitness Coaching",
        "description": "Coach-led fitness instruction aesthetic with bright studio gym, clean form demos, and supportive piece-to-camera. Built for fitness brand films, training apps, and coaching content.",
        "category": "commercial",
        "tags": ["fitness", "coaching", "gym", "training", "instruction"],
        "promptSuffix": " Art style: Bright clean fitness studio with light wood floors, mirrors, and minimal equipment. Coach demonstrates exercises with proper form, talks to camera between sets, supports a client through a movement. Apparel branded but not loud. Mood: Motivating, supportive, capable. Lighting: Bright even studio LED supplemented by skylight or large windows. Coach lit cleanly from the front, slight rim from behind. No harsh shadow. Camera: Mix of locked head-and-shoulders coach pieces, wide form-check angles, and macro detail of grip and footwork. Smooth gimbal moves between camera positions. Color grading: Bright and energetic. One brand accent. Skin tones warm and healthy. Whites kept clean. Looks like a premium training app. Inspired by: connected-fitness instructor reels, studio fitness-app spots, smart-gym workout demos, training-app coaching sessions.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Motivating, supportive, capable",
                "artStyle": "Bright clean fitness studio with light wood floors, mirrors, and minimal equipment. Coach demonstrates exercises with proper form, talks to camera between sets, supports a client through a movement. Apparel branded but not loud",
                "lighting": "Bright even studio LED supplemented by skylight or large windows. Coach lit cleanly from the front, slight rim from behind. No harsh shadow",
                "colorGrading": "Bright and energetic. One brand accent. Skin tones warm and healthy. Whites kept clean. Looks like a premium training app",
                "colorPalette": ["#F8F4ED", "#FF4F1F", "#1F1F1F", "#8FA98D", "#FFFFFF"],
            },
            "motion": {
                "camera": "Mix of locked head-and-shoulders coach pieces, wide form-check angles, and macro detail of grip and footwork. Smooth gimbal moves between camera positions"
            },
            "references": [
                "connected-fitness instructor reels",
                "studio fitness-app spots",
                "smart-gym workout demos",
                "training-app coaching sessions",
            ],
        },
    },
    {
        "id": "edtech-explainer",
        "label": "Edtech Explainer",
        "description": "Friendly edtech explainer aesthetic that combines learner moments with motion-graphic UI inserts and supportive narration. Built for course launches, learning apps, and education brand films.",
        "category": "tech",
        "tags": ["edtech", "education", "explainer", "course", "learning"],
        "promptSuffix": " Art style: Edtech storytelling intercutting diverse learners at desks, in cafés, and on phones with friendly UI inserts of the learning platform. Course cards, progress bars, and instructor avatars float in clean glass-card UI. Wardrobe casual, environments real. Mood: Friendly, encouraging, clear. Lighting: Soft natural daylight for learners. UI inserts rendered with subtle inner glow. Never harsh, always inviting. Camera: Locked or gently floated handheld for learners. UI inserts flat-on with measured push-ins to specific course elements. Cuts driven by narration. Color grading: Bright and warm. Skin tones honest, UI surfaces rendered cleanly. One brand accent punches. Looks like a friendly app launch. Inspired by: playful language-app brand films, nonprofit learning-platform promos, online-course learner stories, cinematic expert-class trailers.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Friendly, encouraging, clear",
                "artStyle": "Edtech storytelling intercutting diverse learners at desks, in cafés, and on phones with friendly UI inserts of the learning platform. Course cards, progress bars, and instructor avatars float in clean glass-card UI. Wardrobe casual, environments real",
                "lighting": "Soft natural daylight for learners. UI inserts rendered with subtle inner glow. Never harsh, always inviting",
                "colorGrading": "Bright and warm. Skin tones honest, UI surfaces rendered cleanly. One brand accent punches. Looks like a friendly app launch",
                "colorPalette": ["#FFF7E8", "#FF8C42", "#1F2A4A", "#8AC926", "#FFFFFF"],
            },
            "motion": {
                "camera": "Locked or gently floated handheld for learners. UI inserts flat-on with measured push-ins to specific course elements. Cuts driven by narration"
            },
            "references": [
                "playful language-app brand films",
                "nonprofit learning-platform promos",
                "online-course learner stories",
                "cinematic expert-class trailers",
            ],
        },
    },
    {
        "id": "automotive-showroom",
        "label": "Automotive Showroom",
        "description": "Dealership and showroom cinematography that lets the car shine in a controlled retail environment. Built for dealer reels, inventory videos, and certified pre-owned spots.",
        "category": "commercial",
        "tags": ["automotive", "showroom", "dealer", "inventory", "retail"],
        "promptSuffix": " Art style: Showroom floor with polished concrete, bright overhead light, brand signage. Vehicle photographed from a slow walk-around. Interior reveals -- dashboard, infotainment, seats. Salesperson occasionally visible in soft focus. Mood: Inviting, confident, retail-grade. Lighting: Bright overhead retail LED with diffuse soft top. Reflections on paint controlled with diffusion overhead. Interior dashboard glow rendered cleanly. Camera: Slow gimbal walk-around. Locked wides for hero. Macro for badge, stitching, and dash detail. Beats paced for retail browsing. Color grading: Bright, clean, with faithful paint color. Whites neutral. Looks like a retail-tour spot. Inspired by: used-car marketplace inventory videos, online car-listing films, luxury-marque dealer spots, minimalist EV showroom reveal films.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Inviting, confident, retail-grade",
                "artStyle": "Showroom floor with polished concrete, bright overhead light, brand signage. Vehicle photographed from a slow walk-around. Interior reveals -- dashboard, infotainment, seats. Salesperson occasionally visible in soft focus",
                "lighting": "Bright overhead retail LED with diffuse soft top. Reflections on paint controlled with diffusion overhead. Interior dashboard glow rendered cleanly",
                "colorGrading": "Bright, clean, with faithful paint color. Whites neutral. Looks like a retail-tour spot",
                "colorPalette": ["#F8F4ED", "#1F2A40", "#C0392B", "#9CA3AF", "#FFFFFF"],
            },
            "motion": {
                "camera": "Slow gimbal walk-around. Locked wides for hero. Macro for badge, stitching, and dash detail. Beats paced for retail browsing"
            },
            "references": [
                "used-car marketplace inventory videos",
                "online car-listing films",
                "luxury-marque dealer spots",
                "minimalist EV showroom reveal films",
            ],
        },
    },
    {
        "id": "b2b-keynote",
        "label": "B2B Keynote",
        "description": "Conference-stage keynote aesthetic with executive on a clean stage, audience reverse-cuts, and brand-controlled stagecraft. Built for enterprise launches, summits, and CEO addresses.",
        "category": "tech",
        "tags": ["b2b", "keynote", "conference", "executive", "summit"],
        "promptSuffix": " Art style: Executive on a minimal stage with a single brand mark behind. Audience reverse cuts catch attention and applause. Cuts to product reveals or content slides full-frame. Wardrobe considered, never showy. Mood: Authoritative, polished, brand-controlled. Lighting: Clean theatrical key on the presenter with soft fill. Stage backlight glow. Audience in soft ambient haze. Reveal lighting for hero product moments. Camera: Three-camera coverage -- wide, medium, close. Slow lens-zoom punch-ins. Cuts to slides full-frame. Beats paced for live keynote. Color grading: Cool, contrasty, with clean blacks and faithful skin tones. Stage glow rendered without bloom. Looks like a live broadcast feed. Inspired by: flagship tech keynote events, enterprise-software conference keynotes, cloud-conference mainstage productions, big-ideas conference mainstage talks.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Authoritative, polished, brand-controlled",
                "artStyle": "Executive on a minimal stage with a single brand mark behind. Audience reverse cuts catch attention and applause. Cuts to product reveals or content slides full-frame. Wardrobe considered, never showy",
                "lighting": "Clean theatrical key on the presenter with soft fill. Stage backlight glow. Audience in soft ambient haze. Reveal lighting for hero product moments",
                "colorGrading": "Cool, contrasty, with clean blacks and faithful skin tones. Stage glow rendered without bloom. Looks like a live broadcast feed",
                "colorPalette": ["#0A0A0F", "#1E2A3A", "#FFFFFF", "#C8A464", "#A8AFB8"],
            },
            "motion": {
                "camera": "Three-camera coverage -- wide, medium, close. Slow lens-zoom punch-ins. Cuts to slides full-frame. Beats paced for live keynote"
            },
            "references": [
                "flagship tech keynote events",
                "enterprise-software conference keynotes",
                "cloud-conference mainstage productions",
                "big-ideas conference mainstage talks",
            ],
        },
    },
    {
        "id": "nonprofit-cause",
        "label": "Nonprofit Cause",
        "description": "Cause-led nonprofit storytelling aesthetic with documentary intimacy, dignified portraits, and supportive context. Built for fundraising reels, impact reports, and campaign films.",
        "category": "commercial",
        "tags": ["nonprofit", "cause", "fundraising", "impact", "documentary"],
        "promptSuffix": " Art style: Documentary portraits of beneficiaries, volunteers, and program staff in real settings -- a community center, a field, a classroom. Subjects framed with respect, never as victims. Cutaways to environmental detail and impact data. Mood: Hopeful, dignified, urgent. Lighting: Available natural light supplemented by minimal reflector. Practicals at evening. Faces always dignified. No harsh shadow. Camera: Light handheld for interview and observation. Locked frames for portraits. Slow wide reveals of environment. Beats let subjects breathe. Color grading: Warm naturalism. Skin tones honest, shadows luminous. Subtle filmic finish. Looks like a respectful documentary short. Inspired by: clean-water nonprofit brand films, humanitarian field-medicine spots, global children’s-aid campaign reels, water-access documentary cuts.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Hopeful, dignified, urgent",
                "artStyle": "Documentary portraits of beneficiaries, volunteers, and program staff in real settings -- a community center, a field, a classroom. Subjects framed with respect, never as victims. Cutaways to environmental detail and impact data",
                "lighting": "Available natural light supplemented by minimal reflector. Practicals at evening. Faces always dignified. No harsh shadow",
                "colorGrading": "Warm naturalism. Skin tones honest, shadows luminous. Subtle filmic finish. Looks like a respectful documentary short",
                "colorPalette": ["#F4E4CF", "#A4805D", "#3F3F3F", "#8FA98D", "#FFFFFF"],
            },
            "motion": {
                "camera": "Light handheld for interview and observation. Locked frames for portraits. Slow wide reveals of environment. Beats let subjects breathe"
            },
            "references": [
                "clean-water nonprofit brand films",
                "humanitarian field-medicine spots",
                "global children’s-aid campaign reels",
                "water-access documentary cuts",
            ],
        },
    },
    {
        "id": "travel-destination",
        "label": "Travel Destination",
        "description": "Aspirational travel destination cinematography with sweeping landscapes, intimate cultural moments, and dawn-to-dusk magic. Built for tourism boards, hotel groups, and travel brand films.",
        "category": "commercial",
        "tags": ["travel", "destination", "tourism", "landscape", "culture"],
        "promptSuffix": " Art style: Travel cinematography that alternates between sweeping landscape aerials and intimate cultural moments -- a hand pouring tea, a fisherman at dawn, lanterns lit at dusk. Locations rendered with respect and specificity, never generic. Mood: Aspirational, romantic, expansive. Lighting: Warm low sun favored. Magic hour for hero exteriors, soft window light for intimate moments, candle and lantern glow at night. Big sky drama embraced. Camera: Drone reveal aerials, slow tracking landscape moves, intimate handheld portraits. Long beats let the place breathe. Cuts paced like a magazine essay. Color grading: Warm filmic with rich saturation. Skies pushed lightly toward blue, skin tones golden. Highlights bloomed gently. Looks like a travel-magazine spread. Inspired by: monochrome humanist photo-essay documentary, wandering-chef travelogue intimacy, home-stay travel brand films, national tourism-board spots.",
        "config": {
            "version": 2,
            "look": {
                "mood": "Aspirational, romantic, expansive",
                "artStyle": "Travel cinematography that alternates between sweeping landscape aerials and intimate cultural moments -- a hand pouring tea, a fisherman at dawn, lanterns lit at dusk. Locations rendered with respect and specificity, never generic",
                "lighting": "Warm low sun favored. Magic hour for hero exteriors, soft window light for intimate moments, candle and lantern glow at night. Big sky drama embraced",
                "colorGrading": "Warm filmic with rich saturation. Skies pushed lightly toward blue, skin tones golden. Highlights bloomed gently. Looks like a travel-magazine spread",
                "colorPalette": ["#F4D9A8", "#7F3F2C", "#E8C28C", "#3F4F5F", "#FFFFFF"],
            },
            "motion": {
                "camera": "Drone reveal aerials, slow tracking landscape moves, intimate handheld portraits. Long beats let the place breathe. Cuts paced like a magazine essay"
            },
            "references": [
                "monochrome humanist photo-essay documentary",
                "wandering-chef travelogue intimacy",
                "home-stay travel brand films",
                "national tourism-board spots",
            ],
        },
    },
]


# Style category keys → human labels (mirrors openstory STYLE_CATEGORY_LABELS).
CATEGORY_LABELS: dict[str, str] = {
    "commercial": "Commercial",
    "ecommerce": "E-commerce",
    "influencer": "Influencer & UGC",
    "film": "Film & Cinematic",
    "animation": "Animation",
    "animatic": "Animatic & Previz",
    "kids": "Kids",
    "tech": "Tech",
    "specialized": "Specialized",
}


def style_category_label(category: str | None) -> str:
    """Friendly heading for a style category (title-cases unknown values)."""
    if not category:
        return "Other"
    return CATEGORY_LABELS.get(category, category[:1].upper() + category[1:])


__all__ = ["CURATED_STYLES", "CATEGORY_LABELS", "style_category_label"]
