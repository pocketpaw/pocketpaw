<script lang="ts">
  import Grid3x3 from "@lucide/svelte/icons/grid-3x3";
  import Box from "@lucide/svelte/icons/box";
  import Sun from "@lucide/svelte/icons/sun";
  import RotateCcw from "@lucide/svelte/icons/rotate-ccw";
  import Loader2 from "@lucide/svelte/icons/loader-2";
  import AlertCircle from "@lucide/svelte/icons/alert-circle";
  import { cn } from "$lib/utils";
  import { onMount } from "svelte";

  let {
    src,
    extension = "stl",
  }: {
    src: string;
    extension?: string;
  } = $props();

  let containerEl = $state<HTMLDivElement | null>(null);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let wireframe = $state(false);
  let showGrid = $state(true);
  let darkBg = $state(true);

  let scene: any;
  let camera: any;
  let renderer: any;
  let controls: any;
  let gridHelper: any;
  let modelGroup: any;
  let animationId: number;
  let resizeObserver: ResizeObserver | null = null;

  // Store initial camera state for reset
  let initialCameraPos = { x: 3, y: 3, z: 3 };

  function toggleWireframe() {
    wireframe = !wireframe;
    if (modelGroup) {
      modelGroup.traverse((child: any) => {
        if (child.isMesh && child.material) {
          if (Array.isArray(child.material)) {
            child.material.forEach((m: any) => { m.wireframe = wireframe; });
          } else {
            child.material.wireframe = wireframe;
          }
        }
      });
    }
  }

  function toggleGrid() {
    showGrid = !showGrid;
    if (gridHelper) gridHelper.visible = showGrid;
  }

  function toggleBackground() {
    darkBg = !darkBg;
    if (scene) {
      const THREE = (window as any).__THREE_REF;
      if (THREE) {
        scene.background = new THREE.Color(darkBg ? 0x1a1a2e : 0xf5f5f5);
      }
    }
  }

  function resetView() {
    if (camera && controls) {
      camera.position.set(initialCameraPos.x, initialCameraPos.y, initialCameraPos.z);
      camera.lookAt(0, 0, 0);
      controls.reset();
    }
  }

  onMount(() => {
    if (!containerEl) return;

    (async () => {
      try {
        const THREE = await import("three");
        const { OrbitControls } = await import("three/examples/jsm/controls/OrbitControls.js");

        // Store reference for background toggle
        (window as any).__THREE_REF = THREE;

        // Scene setup
        scene = new THREE.Scene();
        scene.background = new THREE.Color(darkBg ? 0x1a1a2e : 0xf5f5f5);

        // Camera
        camera = new THREE.PerspectiveCamera(
          50,
          containerEl!.clientWidth / containerEl!.clientHeight,
          0.01,
          1000,
        );
        camera.position.set(initialCameraPos.x, initialCameraPos.y, initialCameraPos.z);

        // Renderer
        renderer = new THREE.WebGLRenderer({ antialias: true });
        renderer.setPixelRatio(window.devicePixelRatio);
        renderer.setSize(containerEl!.clientWidth, containerEl!.clientHeight);
        containerEl!.appendChild(renderer.domElement);

        // Controls
        controls = new OrbitControls(camera, renderer.domElement);
        controls.enableDamping = true;
        controls.dampingFactor = 0.1;
        controls.saveState();

        // Lights
        const ambient = new THREE.AmbientLight(0xffffff, 0.6);
        scene.add(ambient);
        const directional = new THREE.DirectionalLight(0xffffff, 0.8);
        directional.position.set(5, 10, 7);
        scene.add(directional);

        // Grid
        gridHelper = new THREE.GridHelper(10, 20, 0x444444, 0x333333);
        scene.add(gridHelper);

        // Load model
        const ext = extension.toLowerCase();
        let loadedObject: any;

        if (ext === "stl") {
          const { STLLoader } = await import("three/examples/jsm/loaders/STLLoader.js");
          const loader = new STLLoader();
          const geometry = await new Promise<any>((resolve, reject) => {
            loader.load(src, resolve, undefined, reject);
          });
          const material = new THREE.MeshStandardMaterial({
            color: 0x7c93c3,
            metalness: 0.2,
            roughness: 0.6,
            wireframe,
          });
          loadedObject = new THREE.Mesh(geometry, material);
        } else if (ext === "obj") {
          const { OBJLoader } = await import("three/examples/jsm/loaders/OBJLoader.js");
          const loader = new OBJLoader();
          loadedObject = await new Promise<any>((resolve, reject) => {
            loader.load(src, resolve, undefined, reject);
          });
        } else if (ext === "gltf" || ext === "glb") {
          const { GLTFLoader } = await import("three/examples/jsm/loaders/GLTFLoader.js");
          const loader = new GLTFLoader();
          const gltf = await new Promise<any>((resolve, reject) => {
            loader.load(src, resolve, undefined, reject);
          });
          loadedObject = gltf.scene;
        }

        if (loadedObject) {
          modelGroup = loadedObject;

          // Auto-center and scale
          const box = new THREE.Box3().setFromObject(loadedObject);
          const center = box.getCenter(new THREE.Vector3());
          const size = box.getSize(new THREE.Vector3());
          const maxDim = Math.max(size.x, size.y, size.z);
          const scale = maxDim > 0 ? 2 / maxDim : 1;

          loadedObject.position.sub(center);
          loadedObject.scale.multiplyScalar(scale);

          scene.add(loadedObject);

          // Adjust camera based on model size
          const dist = 4;
          camera.position.set(dist, dist, dist);
          camera.lookAt(0, 0, 0);
          initialCameraPos = { x: dist, y: dist, z: dist };
          controls.saveState();
        }

        loading = false;

        // Animation loop
        function animate() {
          animationId = requestAnimationFrame(animate);
          controls.update();
          renderer.render(scene, camera);
        }
        animate();

        // Resize observer
        resizeObserver = new ResizeObserver(() => {
          if (!containerEl || !renderer || !camera) return;
          const w = containerEl.clientWidth;
          const h = containerEl.clientHeight;
          camera.aspect = w / h;
          camera.updateProjectionMatrix();
          renderer.setSize(w, h);
        });
        resizeObserver.observe(containerEl!);
      } catch (e) {
        error = e instanceof Error ? e.message : String(e);
        loading = false;
      }
    })();

    return () => {
      if (animationId) cancelAnimationFrame(animationId);
      if (resizeObserver) resizeObserver.disconnect();
      if (renderer) {
        renderer.dispose();
        if (renderer.domElement?.parentElement) {
          renderer.domElement.parentElement.removeChild(renderer.domElement);
        }
      }
      delete (window as any).__THREE_REF;
    };
  });
</script>

<div class="flex h-full flex-col">
  <!-- Toolbar -->
  <div class="flex items-center gap-1 border-b border-border/50 px-3 py-1.5">
    <button
      type="button"
      class={cn(
        "flex items-center gap-1 rounded-md px-2 py-1 text-xs transition-colors",
        wireframe ? "bg-primary/20 text-primary" : "text-muted-foreground hover:bg-muted hover:text-foreground",
      )}
      onclick={toggleWireframe}
      title="Toggle wireframe"
    >
      <Box class="h-3.5 w-3.5" />
      Wireframe
    </button>
    <button
      type="button"
      class={cn(
        "flex items-center gap-1 rounded-md px-2 py-1 text-xs transition-colors",
        showGrid ? "bg-primary/20 text-primary" : "text-muted-foreground hover:bg-muted hover:text-foreground",
      )}
      onclick={toggleGrid}
      title="Toggle grid"
    >
      <Grid3x3 class="h-3.5 w-3.5" />
      Grid
    </button>
    <button
      type="button"
      class={cn(
        "flex items-center gap-1 rounded-md px-2 py-1 text-xs transition-colors",
        "text-muted-foreground hover:bg-muted hover:text-foreground",
      )}
      onclick={toggleBackground}
      title="Toggle background"
    >
      <Sun class="h-3.5 w-3.5" />
      {darkBg ? "Light" : "Dark"}
    </button>

    <div class="mx-2 h-4 w-px bg-border/50"></div>

    <button
      type="button"
      class="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
      onclick={resetView}
      title="Reset view"
    >
      <RotateCcw class="h-3.5 w-3.5" />
      Reset
    </button>

    <span class="ml-auto text-xs text-muted-foreground uppercase">
      {extension}
    </span>
  </div>

  <!-- 3D viewport -->
  <div class="relative flex-1 overflow-hidden" bind:this={containerEl}>
    {#if loading}
      <div class="absolute inset-0 flex items-center justify-center bg-background/80 z-10">
        <div class="flex flex-col items-center gap-2 text-muted-foreground">
          <Loader2 class="h-8 w-8 animate-spin" />
          <span class="text-sm">Loading 3D model...</span>
        </div>
      </div>
    {/if}
    {#if error}
      <div class="absolute inset-0 flex items-center justify-center bg-background/80 z-10">
        <div class="flex flex-col items-center gap-3 text-muted-foreground">
          <AlertCircle class="h-8 w-8 text-red-400" />
          <span class="text-sm">Failed to load model</span>
          <p class="max-w-md text-center text-xs text-red-400/80">{error}</p>
        </div>
      </div>
    {/if}
  </div>
</div>
