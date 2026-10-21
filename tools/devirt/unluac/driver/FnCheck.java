import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;
import unluac.Configuration;
import unluac.Main;
import unluac.decompile.Decompiler;
import unluac.decompile.Output;
import unluac.decompile.OutputProvider;
import unluac.parse.LAbsLineInfo;
import unluac.parse.LFunction;
import unluac.parse.LLocal;
import unluac.parse.LObject;

/**
 * Decompile every function of a chunk in isolation (children replaced by stubs) and report the
 * ones unluac rejects. Usage: java FnCheck <in.luac> [finder|default]
 * Output: one line per failing function: "FAIL <preorder index> <exception> <top frame>".
 */
public class FnCheck {
  static final int RETURN_0_1 = 30 | (1 << 23);

  static void preorder(LFunction f, List<LFunction> out) {
    out.add(f);
    for (LFunction c : f.functions) preorder(c, out);
  }

  static LFunction stub(LFunction parent, LFunction c) {
    LFunction s = new LFunction(c.header, c.name, c.linedefined, c.lastlinedefined,
        new int[] {RETURN_0_1}, new int[0], new LAbsLineInfo[0], new LLocal[0], new LObject[0],
        c.upvalues, new LFunction[0], 2, c.numUpvalues, 0, 0);
    s.parent = parent;
    s.stripped = c.stripped;
    s.level = c.level;
    return s;
  }

  public static void main(String[] args) throws Exception {
    Configuration config = new Configuration();
    String mode = args.length > 1 ? args[1] : "patched";
    if (mode.equals("finder")) config.variable = Configuration.VariableMode.FINDER;
    Method m = Main.class.getDeclaredMethod("file_to_function", String.class, Configuration.class);
    m.setAccessible(true);
    LFunction main = (LFunction) m.invoke(null, args[0], config);
    if (mode.equals("patched")) Patch.allowGoto(main.header.version);
    if (mode.equals("nogoto")) Patch.allowBreakRewrite(main.header.version);
    List<LFunction> all = new ArrayList<>();
    preorder(main, all);
    OutputProvider sink = new OutputProvider() {
      public void print(String s) {}
      public void print(byte b) {}
      public void println() {}
      public void finish() {}
    };
    int fails = 0;
    for (int i = 0; i < all.size(); i++) {
      LFunction f = all.get(i);
      LFunction[] kids = f.functions;
      LFunction[] stubs = new LFunction[kids.length];
      for (int k = 0; k < kids.length; k++) stubs[k] = stub(f, kids[k]);
      f.functions = stubs;
      try {
        if (mode.equals("hybrid")) f.header.config.variable = Configuration.VariableMode.DEFAULT;
        Decompiler d = new Decompiler(f);
        if (mode.equals("hybrid")) f.header.config.variable = Configuration.VariableMode.NODEBUG;
        Decompiler.State st = d.decompile();
        d.print(st, new Output(sink));
      } catch (Throwable t) {
        fails++;
        StackTraceElement[] tr = t.getStackTrace();
        String where = tr.length > 0 ? tr[0].getClassName() + "." + tr[0].getMethodName() + ":" + tr[0].getLineNumber() : "?";
        String where2 = tr.length > 1 ? tr[1].getMethodName() + ":" + tr[1].getLineNumber() : "";
        System.out.println("FAIL " + i + " " + t + " " + where + " " + where2);
      } finally {
        f.functions = kids;
      }
    }
    System.out.println("checked " + all.size() + " functions, failed " + fails);
  }
}
