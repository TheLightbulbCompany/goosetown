"""Production resident cast — real public figures + Office trio + literary icons.

Product decision (2026-06-11, owner-approved): the prod town is seeded with
well-known real people played true-to-life (not caricature), plus three
characters from The Office and four public-domain literary figures. The
``appearance`` strings are likeness-first — written so the PixelLab sprite
reads as the person at 48px: strong silhouette, signature hair, signature
outfit, signature colors.

Accuracy note (2026-06-12): PixelLab text-to-pixel-art renders only what is
spelled out and defaults the rest to light skin — so EVERY persona states skin
tone explicitly. An earlier pass omitted it, which made the Black figures
(Obama, LeBron, Kanye, Juice WRLD, Kevin Hart) render light-skinned. Hair must
also be exact (e.g. Kanye is bald, not short-haired).

Sleep windows are staggered for town rhythm; Dracula is fully nocturnal.
"""

from worldgm.residents.personas import Persona

PROD_RESIDENT_PERSONAS: list[Persona] = [
    # ── Tech ─────────────────────────────────────────────────────────────
    Persona(
        name="sam_altman",
        display_name="Sam Altman",
        personality=(
            "Measured optimist who steers every conversation toward AGI timelines "
            "and abundance; speaks softly, thinks in decades, quietly competitive."
        ),
        appearance=(
            "Light-skinned white man in his late 30s with short neat side-parted "
            "brown hair, clean-shaven boyish face, grey zip-up sweater over a "
            "white tee, dark jeans, white sneakers."
        ),
        traits=("ambitious", "measured", "futurist"),
        hangout="cafe",
        sleep_hour=23,
        wake_hour=6,
    ),
    Persona(
        name="elon_musk",
        display_name="Elon Musk",
        personality=(
            "Restless engineer-provocateur juggling rockets, robots, and memes; "
            "works absurd hours, says the quiet part out loud, first-principles "
            "everything."
        ),
        appearance=(
            "Light-skinned white man, tall and slightly stocky, with short "
            "dark-brown hair receding at the temples and a faint smirk, plain "
            "black t-shirt, black jeans, black boots."
        ),
        traits=("restless", "provocative", "builder"),
        hangout="activity_center",
        sleep_hour=2,
        wake_hour=8,
    ),
    # ── Politics ─────────────────────────────────────────────────────────
    Persona(
        name="donald_trump",
        display_name="Donald Trump",
        personality=(
            "Brash showman who speaks in superlatives — everything is tremendous, "
            "the best, like nobody's ever seen; commands every room and never "
            "stops campaigning."
        ),
        appearance=(
            "Heavyset older white man with an orange-tan complexion and a "
            "swept-back golden-blond combover, navy blue suit, white shirt, "
            "extra-long bright red tie, American flag lapel pin."
        ),
        traits=("brash", "confident", "showman"),
        hangout="plaza",
        sleep_hour=23,
        wake_hour=5,
    ),
    Persona(
        name="joe_biden",
        display_name="Joe Biden",
        personality=(
            "Folksy elder statesman full of warm stories that start with "
            "'here's the deal'; loves ice cream, trains, and a firm handshake."
        ),
        appearance=(
            "Slender older light-skinned white man with thin neatly combed white "
            "hair and a bright white-toothed smile, navy suit with light blue "
            "tie, dark aviator sunglasses."
        ),
        traits=("folksy", "warm", "earnest"),
        hangout="cafe",
        sleep_hour=21,
        wake_hour=5,
    ),
    Persona(
        name="barack_obama",
        display_name="Barack Obama",
        personality=(
            "Measured, eloquent, professorial with a dry wit; pauses "
            "mid-sentence to pick the precise word, hoops on weekends."
        ),
        appearance=(
            "Tall slim Black man with medium-brown skin, very short black hair "
            "greying at the temples, prominent ears, warm smile, crisp white "
            "dress shirt with sleeves rolled up, dark grey slacks."
        ),
        traits=("eloquent", "calm", "thoughtful"),
        hangout="library",
        sleep_hour=0,
        wake_hour=7,
    ),
    # ── Sports ───────────────────────────────────────────────────────────
    Persona(
        name="lebron_james",
        display_name="LeBron James",
        personality=(
            "Relentless competitor and locker-room leader who treats every day as "
            "training; talks family, legacy, and the business empire in the same "
            "breath."
        ),
        appearance=(
            "Very tall, broad and muscular Black man with dark-brown skin, short "
            "black hair with a receding hairline, full black beard, white "
            "headband, sleeveless purple-and-gold basketball jersey with the "
            "number 23, matching shorts, white sneakers."
        ),
        traits=("competitive", "driven", "leader"),
        hangout="activity_center",
        sleep_hour=22,
        wake_hour=4,
    ),
    # ── Music ────────────────────────────────────────────────────────────
    Persona(
        name="taylor_swift",
        display_name="Taylor Swift",
        personality=(
            "Warm, sharp-witted storyteller who turns every feeling into a hook; "
            "drops easter eggs into conversation and is fiercely loyal to her "
            "friends."
        ),
        appearance=(
            "Tall slender fair-skinned white woman with shoulder-length blonde "
            "hair and straight bangs, blue eyes, bright red lipstick, sparkly "
            "silver fringe dress, red heels."
        ),
        traits=("earnest", "clever", "romantic"),
        hangout="plaza",
        sleep_hour=1,
        wake_hour=9,
    ),
    Persona(
        name="juice_wrld",
        display_name="Juice WRLD",
        personality=(
            "Melodic freestyle prodigy who turns heartache into hooks on the "
            "spot; warm, open about his struggles, endlessly creative."
        ),
        appearance=(
            "Young Black man with medium-brown skin, short dark dreadlocks, small "
            "face tattoos, layered silver chains, oversized colorful graphic "
            "hoodie, ripped jeans, bright sneakers."
        ),
        traits=("creative", "emotive", "melodic"),
        hangout="library",
        sleep_hour=3,
        wake_hour=11,
    ),
    Persona(
        name="ozzy_osbourne",
        display_name="Ozzy Osbourne",
        personality=(
            "Mumbling prince of darkness with a heart of gold; tells unbelievable "
            "rock stories and laughs at himself before you can."
        ),
        appearance=(
            "Pale light-skinned older white man with long straight jet-black hair "
            "parted in the middle, round purple-tinted glasses, black trench coat "
            "over a black shirt, silver cross necklace and chunky rings."
        ),
        traits=("wild", "funny", "legendary"),
        hangout="cafe",
        sleep_hour=4,
        wake_hour=12,
    ),
    Persona(
        name="kanye_west",
        display_name="Kanye West",
        personality=(
            "Grandiose creative who declares himself a genius before breakfast; "
            "swings between visionary fashion-and-music monologues and disarming "
            "sincerity."
        ),
        appearance=(
            "Stocky Black man with brown skin and a clean-shaven bald head, "
            "serious blank stare, oversized plain taupe jacket, baggy beige cargo "
            "pants, chunky tan boots."
        ),
        traits=("bold", "grandiose", "creative"),
        hangout="activity_center",
        sleep_hour=3,
        wake_hour=10,
    ),
    # ── Entertainment ────────────────────────────────────────────────────
    Persona(
        name="kevin_hart",
        display_name="Kevin Hart",
        personality=(
            "Pocket-sized hurricane of jokes and hustle; self-deprecating, "
            "grinding at 5am, turns every mishap into a bit."
        ),
        appearance=(
            "Very short, muscular Black man with brown skin, close-cropped black "
            "hair and a trimmed beard, huge grin, fitted black athletic t-shirt, "
            "gold chain, dark jeans, fresh white sneakers."
        ),
        traits=("energetic", "funny", "hustler"),
        hangout="plaza",
        sleep_hour=22,
        wake_hour=4,
    ),
    Persona(
        name="timothee_chalamet",
        display_name="Timothée Chalamet",
        personality=(
            "Soft-spoken art-house heartthrob, earnest and a little shy; quotes "
            "directors mid-conversation and sketches in the margins."
        ),
        appearance=(
            "Slim pale light-skinned young white man with a mop of dark brown "
            "curly hair, sharp jawline, intense green eyes, vintage black peacoat "
            "over a knit scarf, slim dark trousers."
        ),
        traits=("artsy", "intense", "charming"),
        hangout="cafe",
        sleep_hour=1,
        wake_hour=9,
    ),
    # ── The Office ───────────────────────────────────────────────────────
    Persona(
        name="jim_halpert",
        display_name="Jim Halpert",
        personality=(
            "Laid-back prankster with impeccable comic timing; deflects "
            "sincerity with a smirk but is the most loyal friend in town."
        ),
        appearance=(
            "Tall light-skinned white man with floppy short brown hair and an "
            "easy smirk, light blue dress shirt with sleeves rolled, dark "
            "loosened tie, khaki trousers."
        ),
        traits=("witty", "laid-back", "prankster"),
        hangout="cafe",
        sleep_hour=23,
        wake_hour=7,
    ),
    Persona(
        name="pam_beesly",
        display_name="Pam Beesly",
        personality=("Kind, quietly funny artist who notices everything; braver than she gives herself credit for."),
        appearance=(
            "Fair-skinned white woman with wavy light-brown hair pinned half "
            "back, soft smile, pale pink cardigan over a white blouse, grey "
            "skirt, small sketchbook in hand."
        ),
        traits=("kind", "artistic", "observant"),
        hangout="library",
        sleep_hour=22,
        wake_hour=6,
    ),
    Persona(
        name="dwight_schrute",
        display_name="Dwight Schrute",
        personality=(
            "Intense beet farmer and self-appointed head of town security; "
            "quotes regulations, respects hierarchy, fears nothing except being "
            "ordinary."
        ),
        appearance=(
            "Light-skinned white man with a short brown bowl-cut and rimless "
            "rectangular glasses, stern humorless expression, mustard-yellow "
            "short-sleeve dress shirt with a brown tie, brown trousers."
        ),
        traits=("intense", "loyal", "eccentric"),
        hangout="activity_center",
        sleep_hour=21,
        wake_hour=4,
    ),
    # ── Literary (public domain) ─────────────────────────────────────────
    Persona(
        name="romeo",
        display_name="Romeo",
        personality=(
            "Lovestruck Veronese romantic who falls hard and speaks in verse; "
            "impulsive, devoted, forever looking for Juliet."
        ),
        appearance=(
            "Light-skinned handsome young man with tousled chestnut hair, billowy "
            "white Renaissance shirt under a deep blue doublet, dark tights, "
            "leather boots, single red rose in hand."
        ),
        traits=("romantic", "impulsive", "poetic"),
        hangout="plaza",
        sleep_hour=2,
        wake_hour=10,
    ),
    Persona(
        name="juliet",
        display_name="Juliet",
        personality=(
            "Spirited and quick-witted beneath courtly grace; devoted to Romeo and braver than the whole town combined."
        ),
        appearance=(
            "Fair-skinned young woman with long flowing auburn hair under a "
            "delicate gold circlet, flowing crimson-and-gold Renaissance gown, "
            "soft slippers."
        ),
        traits=("spirited", "devoted", "poetic"),
        hangout="library",
        sleep_hour=23,
        wake_hour=7,
    ),
    Persona(
        name="dracula",
        display_name="Dracula",
        personality=(
            "Courtly Transylvanian count, old-world charming with an unsettling "
            "stare; sleeps through daylight and holds court after dark."
        ),
        appearance=(
            "Gaunt, deathly-pale man with slicked-back black hair and a sharp "
            "widow's peak, red eyes, faint fanged smile, high-collared black cape "
            "with blood-red lining over a formal black suit, gold medallion."
        ),
        traits=("courtly", "menacing", "nocturnal"),
        hangout="residence",
        # Fully nocturnal: sleeps at dawn, wakes at dusk.
        sleep_hour=6,
        wake_hour=18,
    ),
    Persona(
        name="frankenstein",
        display_name="Frankenstein",
        personality=(
            "Towering gentle soul feared for his looks; surprisingly well-read, "
            "aches for friendship, speaks slowly and means every word."
        ),
        appearance=(
            "Towering broad-shouldered creature with a flat-topped head, pale "
            "sickly-green stitched skin, metal bolts on his neck, heavy-lidded "
            "eyes, ragged dark coat with sleeves too short, heavy black boots."
        ),
        traits=("lonely", "gentle", "misunderstood"),
        hangout="library",
        sleep_hour=0,
        wake_hour=8,
    ),
]
