import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;
import unluac.Configuration;
import unluac.Main;
import unluac.decompile.Decompiler;
import unluac.parse.LFunction;

/**
 * For every function (preorder index) print the 0-based pcs unluac needs a goto label at, using
 * the patched (goto-enabled) Lua 5.1 mode. Children are stubbed so each function is decompiled
 * once. Usage: java LabelDump <in.luac>   Output lines: "<index> <pc> <pc> ..."
 */
public class LabelDump {
  public static void main(String[] args) throws Exception {
    Configuration config = new Configuration();
    Method m = Main.class.getDeclaredMethod("file_to_function", String.class, Configuration.class);
    m.setAccessible(true);
    LFunction main = (LFunction) m.invoke(null, args[0], config);
    Patch.allowGoto(main.header.version);
    List<LFunction> all = new ArrayList<>();
    FnCheck.preorder(main, all);
    Class<?> flagCls = Class.forName("unluac.decompile.Decompiler$Flag");
    int labelBit = flagCls.getField("bit").getInt(flagCls.getField("LABELS").get(null));
    Field flagsField = Decompiler.State.class.getDeclaredField("flags");
    flagsField.setAccessible(true);
    for (int i = 0; i < all.size(); i++) {
      LFunction f = all.get(i);
      LFunction[] kids = f.functions;
      LFunction[] stubs = new LFunction[kids.length];
      for (int k = 0; k < kids.length; k++) stubs[k] = FnCheck.stub(f, kids[k]);
      f.functions = stubs;
      try {
        Decompiler d = new Decompiler(f);
        Decompiler.State st = d.decompile();
        byte[] flags = (byte[]) flagsField.get(st);
        StringBuilder sb = new StringBuilder();
        for (int line = 1; line < flags.length; line++) {
          if ((flags[line] & labelBit) != 0) sb.append(' ').append(line - 1);
        }
        if (sb.length() > 0) System.out.println(i + sb.toString());
      } catch (Throwable t) {
        System.out.println(i + " ERROR " + t);
      } finally {
        f.functions = kids;
      }
    }
  }
}
