export const DEFAULTTILESETPATH = "./tilesets/8bit-tileset.png";

export const tilesetpadding = 0;


export const DEFAULTILEDIMX = 20; // px
export const DEFAULTILEDIMY = 20; // px

export const levelwidth  = 1280; // px (64 tiles * 20px)
export const levelheight = 960;  // px (48 tiles * 20px)

export let leveltilewidth  = Math.floor(levelwidth / DEFAULTILEDIMX);
export let leveltileheight = Math.floor(levelheight / DEFAULTILEDIMX);

export const MAXTILEINDEX = leveltilewidth * leveltileheight;


// -- HTML

export const htmlLayerPaneW = 800;
export const htmlLayerPaneH = 600

export const htmlTilesetPaneW = 800;
export const htmlTilesetPaneH = 600;

export const htmlCompositePaneW = 800;
export const htmlCompositePaneH = 600;

// --  zIndex

// 1-10 taken by layers
export const zIndexFilter           =  20;
export const zIndexMouseShadow      =  30;
export const zIndexGrid             =  50;
export const zIndexCompositePointer =  100;
