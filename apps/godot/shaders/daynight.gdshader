shader_type canvas_item;

// time_of_day: 0.0 = midnight, 0.5 = noon, 1.0 = midnight again
uniform float time_of_day : hint_range(0.0, 1.0) = 0.5;

void fragment() {
	// Deep blue for night, warm orange for dusk/dawn
	vec3 night_color = vec3(0.1, 0.15, 0.35);
	vec3 dusk_color = vec3(0.9, 0.5, 0.2);

	float alpha = 0.0;
	vec3 tint = vec3(1.0);

	if (time_of_day < 0.2) {
		// Night: full dark tint
		tint = night_color;
		alpha = 0.4;
	} else if (time_of_day < 0.3) {
		// Dawn transition: night -> dusk color, fading out
		float t = (time_of_day - 0.2) / 0.1;
		tint = mix(night_color, dusk_color, t);
		alpha = mix(0.4, 0.15, t);
	} else if (time_of_day < 0.7) {
		// Daytime: no overlay
		alpha = 0.0;
	} else if (time_of_day < 0.8) {
		// Dusk transition: fading in warm tint
		float t = (time_of_day - 0.7) / 0.1;
		tint = dusk_color;
		alpha = mix(0.0, 0.15, t);
	} else {
		// Evening: dusk color blending to full night
		float t = (time_of_day - 0.8) / 0.2;
		tint = mix(dusk_color, night_color, t);
		alpha = mix(0.15, 0.4, t);
	}

	COLOR = vec4(tint, alpha);
}
