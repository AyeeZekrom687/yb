import java.io.FileOutputStream;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import unluac.Version;
import unluac.Configuration;
import unluac.Main;
import unluac.decompile.Decompiler;
import unluac.decompile.FileOutputProvider;
import unluac.decompile.Output;
import unluac.parse.LFunction;

/**
 * Run unluac with a chosen variable mode: java Run <in.luac> <out.lua> [nogoto|mixed <idx,idx>|patched|hybrid|finder|default|nodebug]
 * hybrid: declarations come from the chunk's locvars (built while the mode is DEFAULT), then the
 * decompile runs in no-debug mode so unluac accepts luac's jump-threaded break/goto shapes.
 */
public class Run {
  public static void main(String[] args) throws Exception {
    Configuration config = new Configuration();
    String mode = args.length > 2 ? args[2] : "patched";
    if (mode.equals("finder")) config.variable = Configuration.VariableMode.FINDER;
    if (mode.equals("nodebug")) config.variable = Configuration.VariableMode.NODEBUG;
    Method m = Main.class.getDeclaredMethod("file_to_function", String.class, Configuration.class);
    m.setAccessible(true);
    LFunction lmain = (LFunction) m.invoke(null, args[0], config);
    if (mode.equals("patched")) Patch.allowGoto(lmain.header.version);
    if (mode.equals("nogoto")) Patch.allowBreakRewrite(lmain.header.version);
    if (mode.equals("mixed")) {
      // goto-enabled header for the listed functions (preorder indexes), break rewrite for the rest
      Patch.allowBreakRewrite(lmain.header.version);
      unluac.parse.BHeader gh = Patch.copy(lmain.header);
      Version gv = Patch.copy(lmain.header.version);
      Patch.allowGoto(gv);
      Field vf = unluac.parse.BHeader.class.getField("version");
      vf.setAccessible(true);
      vf.set(gh, gv);
      java.util.List<LFunction> all = new java.util.ArrayList<>();
      FnCheck.preorder(lmain, all);
      for (String idx : args[3].split(",")) {
        if (!idx.isEmpty()) all.get(Integer.parseInt(idx)).header = gh;
      }
    }
    Decompiler d = new Decompiler(lmain);
    if (mode.equals("hybrid")) lmain.header.config.variable = Configuration.VariableMode.NODEBUG;
    Decompiler.State result = d.decompile();
    Output output = new Output(new FileOutputProvider(new FileOutputStream(args[1])));
    d.print(result, output);
    output.finish();
  }
}
