export function rectIntersectsViewport(rect, width, height) {
  return rect.right > 0 && rect.bottom > 0 && rect.left < width && rect.top < height;
}
