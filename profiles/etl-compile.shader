// Compile-only shaders for the etl profile (profiles/etl.yaml `compile_shaders`).
//
// Real ET maps use these editor shaders, but nothing the compiler sees defines them. They
// come with the mapper's editor setup, not with the game. Without a definition q3map2 treats
// each as a plain solid, drawn, structural texture, so a terrain's alpha-blend volumes compile
// into solid orange brushes. tools/q3map2.py hands q3map2 the ones nothing else defines on
// every compile (a throwaway pk3 on an extra -fs_pakpath), so a definition the game, the map
// or a pk3 beside it carries always wins. The file is never packed into a map's pk3: nothing
// draws with these shaders, so the packer never asks for them (autopk3.cpp:110).
//
// Sources and confidence are in profiles/etl.yaml `compile_shaders.shaders`.

// q3map2's alphaMod volume terrain blending: a brush with this shader sets the vertex alpha of
// every surface vertex inside it (shaders.cpp:1543-1547, map.cpp:830-835, surface.cpp:2837).
// `volume` must be the first q3map_alphaMod line: map.cpp:833 checks the first colorMod.
textures/editor/alpha_000
{
	qer_trans 0.5
	q3map_alphaMod volume
	q3map_alphaMod set 0
	surfaceparm nodraw
	surfaceparm nonsolid
	surfaceparm trans
	surfaceparm nomarks
}

textures/editor/alpha_100
{
	qer_trans 0.5
	q3map_alphaMod volume
	q3map_alphaMod set 1
	surfaceparm nodraw
	surfaceparm nonsolid
	surfaceparm trans
	surfaceparm nomarks
}

// "exactly like skip, but it filters with hint": the definition ET: Legacy itself ships in
// legacy_v2.86.2.pk3 materials/common.shader, a renderer2 materials file q3map2 never reads.
textures/common/hintskip
{
	qer_editorimage textures/common/skip
	qer_nocarve
	qer_trans 0.30
	surfaceparm nodraw
	surfaceparm nonsolid
	surfaceparm skip
	surfaceparm structural
	surfaceparm trans
}
