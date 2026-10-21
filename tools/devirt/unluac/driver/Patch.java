import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import unluac.Version;

/** Lets the Lua 5.1 decompile emit `goto` and `if ... then break end` rewrites instead of
 *  throwing: luac's jump threading and Luau `continue` produce jumps that need them. */
public class Patch {
  public static void allowGoto(Version v) throws Exception {
    set(v, "usegoto", Boolean.TRUE);
    set(v, "useifbreakrewrite", Boolean.TRUE);
    set(v, "useifgotorewrite", Version.Maybe.YES);
  }

  /** Only the `if cond then break end` rewrite (for luac's threaded break jumps); no goto. */
  public static void allowBreakRewrite(Version v) throws Exception {
    set(v, "useifbreakrewrite", Boolean.TRUE);
  }

  /** Shallow copy of an object without running a constructor (all instance fields, final too). */
  @SuppressWarnings("unchecked")
  public static <T> T copy(T src) throws Exception {
    Field uf = Class.forName("sun.misc.Unsafe").getDeclaredField("theUnsafe");
    uf.setAccessible(true);
    Object unsafe = uf.get(null);
    T dst = (T) unsafe.getClass().getMethod("allocateInstance", Class.class).invoke(unsafe, src.getClass());
    for (Class<?> c = src.getClass(); c != null; c = c.getSuperclass()) {
      for (Field f : c.getDeclaredFields()) {
        if (Modifier.isStatic(f.getModifiers())) continue;
        f.setAccessible(true);
        f.set(dst, f.get(src));
      }
    }
    return dst;
  }

  static void set(Version v, String name, Object value) throws Exception {
    Field f = Version.class.getField(name);
    f.setAccessible(true);
    f.set(v, new Version.Setting<Object>(value));
  }
}
