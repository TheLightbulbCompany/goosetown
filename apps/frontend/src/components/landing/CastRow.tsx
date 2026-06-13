const RESIDENTS = [
  'lucky', 'alice', 'bob', 'knight', 'merchant', 'healer',
  'ranger', 'bard', 'scholar', 'stella', 'tinkerer', 'pete',
];

/**
 * The actual residents, walking in place — each tile is the character's real
 * in-game sprite sheet animated with a CSS steps() walk cycle (south row).
 */
export function CastRow() {
  return (
    <div className="mt-8">
      <p className="font-display text-xs tracking-[0.25em] uppercase text-clay-300">
        current residents
      </p>
      <div className="mt-3 -mx-6 overflow-x-auto px-6 pb-2 landing-scrollbar">
        <div className="flex w-max gap-4 sm:gap-5">
          {RESIDENTS.map((name) => (
            <figure key={name} className="flex flex-col items-center gap-1">
              <div
                className="landing-sprite-walk"
                style={{ backgroundImage: `url(/assets/sprites/${name}-sheet.png)` }}
                role="img"
                aria-label={`${name}, a GooseTown resident`}
              />
              <figcaption className="font-body text-xs text-clay-300">{name}</figcaption>
            </figure>
          ))}
        </div>
      </div>
    </div>
  );
}
